"""API de cobros con Mercado Pago (FastAPI).

Arranque:  .venv/Scripts/python.exe -m app   (o: uvicorn app.main:crear_app --factory)
Docs:      http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import hmac
import json
import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .avisos import Avisador
from .config import Config
from .db import BaseDatos
from .firma import FirmaInvalida, validar_firma
from .mercadopago import ClienteMercadoPago, ErrorMercadoPago
from .servicio import ESTADOS_ORDEN, OperacionInvalida, OrdenNoEncontrada, ServicioPagos, presentar_orden
from .simulador import SimuladorMercadoPago, crear_router

log = logging.getLogger("pagos.api")


# ---------------------------------------------------------------------- modelos de entrada

class ItemEntrada(BaseModel):
    titulo: str = Field(min_length=1, max_length=256, examples=["Remera talle M"])
    cantidad: int = Field(ge=1, le=10_000, examples=[2])
    precio_unitario: Decimal = Field(gt=0, max_digits=12, decimal_places=2, examples=["1500.00"])


class OrdenEntrada(BaseModel):
    items: list[ItemEntrada] = Field(min_length=1, max_length=100)
    comprador_email: str | None = Field(default=None, max_length=254, examples=["cliente@correo.com"])

    @field_validator("comprador_email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v is None or v.strip() == "":
            return None
        v = v.strip()
        if "@" not in v or "." not in v.split("@")[-1] or " " in v:
            raise ValueError("email inválido")
        return v


# ---------------------------------------------------------------------- app

def configurar_logs(nivel: str) -> None:
    logging.basicConfig(level=getattr(logging, nivel, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def crear_app(
    config: Config | None = None,
    transporte_mp: httpx.BaseTransport | None = None,
    avisador: Avisador | None = None,
) -> FastAPI:
    """Fábrica de la app. Las pruebas inyectan config, transporte falso y avisador."""
    config = config or Config.desde_entorno()
    configurar_logs(config.log_nivel)

    simulador = None
    if config.modo == "simulador" and transporte_mp is None:
        simulador = SimuladorMercadoPago(config.base_url_publica, token_esperado=config.access_token)
        transporte_mp = simulador.transporte()

    db = BaseDatos(config.db_path)
    mp = ClienteMercadoPago(config.access_token, config.api_base, config.api_timeout_seg, transporte_mp)
    servicio = ServicioPagos(config, db, mp, avisador or Avisador(config))

    @asynccontextmanager
    async def ciclo_de_vida(_: FastAPI):
        yield
        mp.cerrar()
        db.cerrar()

    app = FastAPI(title="Cobros con Mercado Pago", version="1.0.0", lifespan=ciclo_de_vida,
                  description="Checkout Pro + webhooks firmados + reembolsos. Modo actual: " + config.modo)
    app.state.config = config
    app.state.db = db
    app.state.servicio = servicio
    app.state.simulador = simulador

    log.info("API de pagos iniciada en modo %s (base pública %s)", config.modo, config.base_url_publica)
    if config.modo == "produccion" and not config.admin_api_key:
        log.warning("Producción sin ADMIN_API_KEY: los endpoints /ordenes quedan abiertos")

    # ------------------------------------------------------------ seguridad

    def exigir_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
        if config.admin_api_key and not hmac.compare_digest(x_api_key or "", config.admin_api_key):
            raise HTTPException(401, "falta o es incorrecta la X-API-Key")

    protegido = [Depends(exigir_api_key)]

    # ------------------------------------------------------------ errores

    @app.exception_handler(OrdenNoEncontrada)
    def _no_encontrada(_: Request, exc: OrdenNoEncontrada):
        return JSONResponse(status_code=404, content={"detail": f"no existe la orden {exc}"})

    @app.exception_handler(OperacionInvalida)
    def _invalida(_: Request, exc: OperacionInvalida):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ErrorMercadoPago)
    def _error_mp(_: Request, exc: ErrorMercadoPago):
        # 502: el problema está del lado de Mercado Pago (o de las credenciales).
        return JSONResponse(status_code=502, content={"detail": str(exc), "mercadopago": exc.detalle})

    # ------------------------------------------------------------ endpoints

    @app.get("/salud", tags=["general"], summary="Chequeo de salud")
    def salud():
        return {"estado": "ok", "modo": config.modo, "base_datos": "ok" if db.ping() else "error"}

    @app.post("/ordenes", status_code=201, tags=["ordenes"], dependencies=protegido,
              summary="Crear orden y link de pago (Checkout Pro)")
    def crear_orden(entrada: OrdenEntrada):
        orden = servicio.crear_orden([i.model_dump() for i in entrada.items], entrada.comprador_email)
        return presentar_orden(orden)

    @app.get("/ordenes", tags=["ordenes"], dependencies=protegido, summary="Listar órdenes")
    def listar_ordenes(
        estado: Annotated[str | None, Query(description=f"Uno de {', '.join(ESTADOS_ORDEN)}")] = None,
        limite: Annotated[int, Query(ge=1, le=500)] = 50,
        desde: Annotated[int, Query(ge=0)] = 0,
    ):
        if estado and estado not in ESTADOS_ORDEN:
            raise HTTPException(422, f"estado debe ser uno de {ESTADOS_ORDEN}")
        return [presentar_orden(o) for o in servicio.listar_ordenes(estado, limite, desde)]

    @app.get("/ordenes/{orden_id}", tags=["ordenes"], dependencies=protegido, summary="Ver una orden")
    def ver_orden(orden_id: str):
        return presentar_orden(servicio.ver_orden(orden_id))

    @app.post("/ordenes/{orden_id}/reembolso", tags=["ordenes"], dependencies=protegido,
              summary="Reembolso total del pago aprobado")
    def reembolsar(orden_id: str):
        return presentar_orden(servicio.reembolsar(orden_id))

    @app.post("/webhooks/mercadopago", tags=["webhooks"], summary="Notificaciones de Mercado Pago")
    async def webhook(
        request: Request,
        x_signature: Annotated[str | None, Header()] = None,
        x_request_id: Annotated[str | None, Header()] = None,
    ):
        q = request.query_params
        try:
            cuerpo = await request.json() if await request.body() else {}
        except json.JSONDecodeError:
            cuerpo = {}
        if not isinstance(cuerpo, dict):
            cuerpo = {}
        # Formato webhooks: ?data.id=..&type=payment. Formato viejo IPN: ?id=..&topic=payment
        data_id = q.get("data.id") or str((cuerpo.get("data") or {}).get("id") or "") or q.get("id")
        tipo = q.get("type") or cuerpo.get("type") or q.get("topic")

        if config.webhook_secret:
            try:
                # La firma se calcula con el data.id de la URL (así lo arma Mercado Pago).
                validar_firma(config.webhook_secret, x_signature, x_request_id, q.get("data.id") or data_id,
                              config.firma_tolerancia_seg)
            except FirmaInvalida as exc:
                log.warning("Notificación rechazada (data.id=%s): %s", data_id, exc)
                return JSONResponse(status_code=401, content={"detail": f"firma inválida: {exc}"})

        # Clave de idempotencia: el id de la notificación (se mantiene en los reintentos de Mercado Pago).
        clave = (f"notif:{cuerpo['id']}" if cuerpo.get("id") else
                 f"req:{x_request_id}" if x_request_id else f"{tipo}:{data_id}:{cuerpo.get('action', '')}")
        try:
            # El servicio es síncrono (httpx + sqlite): se corre en un hilo para no bloquear el event loop.
            resultado = await run_in_threadpool(servicio.procesar_notificacion, tipo, data_id, clave)
        except ErrorMercadoPago as exc:
            # Responder != 2xx hace que Mercado Pago reintente más tarde.
            log.error("No se pudo procesar la notificación del pago %s: %s", data_id, exc)
            return JSONResponse(status_code=503 if exc.es_temporal else 500,
                                content={"detail": "no se pudo consultar el pago, reintentar"})
        return resultado

    @app.get("/pago/resultado", response_class=HTMLResponse, tags=["general"],
             summary="Página de vuelta del checkout (back_urls)")
    def resultado_pago(estado: str = "", external_reference: str = ""):
        # Importante: esta página es solo informativa. El estado real lo fija el webhook.
        textos = {"exito": "¡Gracias! Tu pago fue aprobado.", "fallo": "El pago no se pudo completar.",
                  "pendiente": "Tu pago quedó pendiente. Te avisamos cuando se acredite."}
        texto = textos.get(estado, "Volviste del checkout.")
        ref = f"<p>Orden: {external_reference}</p>" if external_reference.isalnum() else ""
        return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Resultado del pago</title>
<style>body{{font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;padding:0 16px}}</style></head>
<body><h1>{texto}</h1>{ref}<p><a href="/">Volver</a></p></body></html>"""

    if simulador:
        app.include_router(crear_router(simulador, config.webhook_secret))

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def inicio():
            return """<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Demo de cobros</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 16px}
button{padding:12px 18px;font-size:16px;cursor:pointer}</style></head><body>
<h1>Demo: cobros con Mercado Pago (simulador)</h1>
<p>1. Creá una orden de prueba. 2. Pagala en el checkout simulado. 3. Mirá cómo el webhook la actualiza.</p>
<button id="b">Crear orden de prueba y pagar</button>
<p><a href="/ordenes">Ver órdenes (JSON)</a> · <a href="/docs">Documentación de la API</a></p>
<script>
document.getElementById('b').onclick = async () => {
  const r = await fetch('/ordenes', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({items: [{titulo: 'Remera de prueba', cantidad: 2, precio_unitario: '1500.00'}],
                          comprador_email: 'comprador@prueba.com'})});
  const o = await r.json();
  if (!r.ok) { alert('Error: ' + JSON.stringify(o)); return; }
  location.href = o.link_pago;
};
</script></body></html>"""

    return app
