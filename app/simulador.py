"""Simulador local de Mercado Pago, para probar todo sin cuenta ni internet.

Tiene dos partes:
  1. SimuladorMercadoPago: imita la API REST (preferencias, pagos, reembolsos) como un transporte
     de httpx. El ClienteMercadoPago lo usa igual que a la API real, sin salir a la red.
  2. Un router de FastAPI (/simulador/...) con una página de "checkout" falsa: el comprador elige
     aprobar, rechazar o dejar pendiente; el simulador crea el pago y manda la notificación FIRMADA
     al webhook, exactamente como lo haría Mercado Pago.

Solo se monta cuando MP_MODO=simulador.
"""

from __future__ import annotations

import html
import itertools
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from .firma import armar_encabezado

log = logging.getLogger("pagos.simulador")

RESULTADOS = {
    "approved": "accredited",
    "rejected": "cc_rejected_insufficient_amount",
    "pending": "pending_waiting_payment",
    "in_process": "pending_contingency",
}


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SimuladorMercadoPago:
    """Estado en memoria de un Mercado Pago de mentira."""

    def __init__(self, base_checkout: str = "http://127.0.0.1:8000", token_esperado: str | None = None):
        self.base_checkout = base_checkout.rstrip("/")
        self.token_esperado = token_esperado
        self.preferencias: dict[str, dict] = {}
        self.pagos: dict[str, dict] = {}
        self.reembolsos_por_clave: dict[str, dict] = {}
        self.pedidos: list[httpx.Request] = []  # para inspeccionar en pruebas
        self._ids = itertools.count(1_000_000_001)
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- transporte httpx

    def transporte(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._manejar)

    def _manejar(self, req: httpx.Request) -> httpx.Response:
        self.pedidos.append(req)
        auth = req.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or (self.token_esperado and auth != f"Bearer {self.token_esperado}"):
            return httpx.Response(401, json={"message": "invalid access token", "status": 401})
        ruta = req.url.path
        if req.method == "POST" and ruta == "/checkout/preferences":
            return self._crear_preferencia(json.loads(req.content or b"{}"))
        if m := re.fullmatch(r"/v1/payments/(\w+)", ruta):
            if req.method == "GET":
                pago = self.pagos.get(m.group(1))
                if not pago:
                    return httpx.Response(404, json={"message": "Payment not found", "status": 404})
                return httpx.Response(200, json=pago)
        if (m := re.fullmatch(r"/v1/payments/(\w+)/refunds", ruta)) and req.method == "POST":
            return self._reembolsar(m.group(1), req.headers.get("x-idempotency-key", ""))
        return httpx.Response(404, json={"message": f"ruta no simulada: {req.method} {ruta}"})

    def _crear_preferencia(self, cuerpo: dict) -> httpx.Response:
        items = cuerpo.get("items") or []
        if not items or any(float(i.get("unit_price", 0)) <= 0 or int(i.get("quantity", 0)) <= 0 for i in items):
            return httpx.Response(400, json={"message": "items invalid", "error": "bad_request", "status": 400})
        pref_id = f"SIM-{uuid.uuid4().hex[:12]}"
        link = f"{self.base_checkout}/simulador/checkout/{pref_id}"
        pref = {**cuerpo, "id": pref_id, "init_point": link, "sandbox_init_point": link,
                "date_created": _ahora()}
        with self._lock:
            self.preferencias[pref_id] = pref
        return httpx.Response(201, json=pref)

    def _reembolsar(self, pago_id: str, clave: str) -> httpx.Response:
        with self._lock:
            if clave and clave in self.reembolsos_por_clave:
                return httpx.Response(201, json=self.reembolsos_por_clave[clave])
            pago = self.pagos.get(pago_id)
            if not pago:
                return httpx.Response(404, json={"message": "Payment not found", "status": 404})
            if pago["status"] != "approved":
                return httpx.Response(400, json={"message": f"Payment status {pago['status']} can't be refunded",
                                                 "status": 400})
            reembolso = {"id": next(self._ids), "payment_id": int(pago_id), "amount": pago["transaction_amount"],
                         "status": "approved", "date_created": _ahora()}
            pago.update(status="refunded", status_detail="refunded", transaction_amount_refunded=pago["transaction_amount"])
            if clave:
                self.reembolsos_por_clave[clave] = reembolso
        return httpx.Response(201, json=reembolso)

    # ---------------------------------------------------------------- acciones del "comprador"

    def pagar(self, pref_id: str, resultado: str = "approved", monto: float | None = None) -> dict:
        """Simula que el comprador paga la preferencia. Devuelve el pago creado."""
        if resultado not in RESULTADOS:
            raise ValueError(f"resultado debe ser uno de {list(RESULTADOS)}")
        pref = self.preferencias.get(pref_id)
        if not pref:
            raise KeyError(pref_id)
        total = round(sum(float(i["unit_price"]) * int(i["quantity"]) for i in pref["items"]), 2)
        with self._lock:
            pago_id = str(next(self._ids))
            pago = {
                "id": int(pago_id),
                "status": resultado,
                "status_detail": RESULTADOS[resultado],
                "external_reference": pref.get("external_reference"),
                "transaction_amount": total if monto is None else monto,
                "currency_id": pref["items"][0].get("currency_id", "ARS"),
                "payment_method_id": "visa",
                "payment_type_id": "credit_card",
                "date_created": _ahora(),
                "date_approved": _ahora() if resultado == "approved" else None,
                "payer": pref.get("payer") or {"email": "comprador@prueba.com"},
                "metadata": pref.get("metadata") or {},
                "preference_id": pref_id,
                "live_mode": False,
            }
            self.pagos[pago_id] = pago
        log.info("[simulador] pago %s creado como %s (preferencia %s)", pago_id, resultado, pref_id)
        return pago

    def armar_notificacion(self, pago_id: str, secreto: str, notification_url: str,
                           action: str = "payment.updated") -> tuple[str, dict, dict]:
        """Devuelve (url, encabezados, cuerpo) de una notificación firmada como las de Mercado Pago."""
        request_id = str(uuid.uuid4())
        cuerpo = {
            "action": action,
            "api_version": "v1",
            "data": {"id": str(pago_id)},
            "date_created": _ahora(),
            "id": next(self._ids),
            "live_mode": False,
            "type": "payment",
            "user_id": "123456789",
        }
        separador = "&" if "?" in notification_url else "?"
        url = f"{notification_url}{separador}{urlencode({'data.id': pago_id, 'type': 'payment'})}"
        headers = {
            "x-signature": armar_encabezado(secreto, str(pago_id), request_id),
            "x-request-id": request_id,
            "content-type": "application/json",
        }
        return url, headers, cuerpo


# -------------------------------------------------------------------- router web del simulador

class PedidoPagoSimulado(BaseModel):
    preferencia_id: str
    resultado: str = "approved"
    monto: float | None = None  # para simular un pago por menos plata
    notificar: bool = True


def crear_router(sim: SimuladorMercadoPago, secreto: str) -> APIRouter:
    router = APIRouter(prefix="/simulador", tags=["simulador"])

    async def enviar_notificacion(request: Request, pago_id: str, notification_url: str, action: str) -> dict:
        """Manda la notificación firmada al webhook de esta misma app (en proceso, sin red)."""
        url, headers, cuerpo = sim.armar_notificacion(pago_id, secreto, notification_url, action)
        partes = urlsplit(url)
        ruta = partes.path + (f"?{partes.query}" if partes.query else "")
        transporte = httpx.ASGITransport(app=request.app)
        async with httpx.AsyncClient(transport=transporte, base_url="http://simulador") as http:
            resp = await http.post(ruta, headers=headers, json=cuerpo)
        log.info("[simulador] notificación del pago %s -> webhook respondió %s", pago_id, resp.status_code)
        return {"status": resp.status_code, "respuesta": resp.json() if resp.content else None,
                "x_request_id": headers["x-request-id"], "notificacion_id": cuerpo["id"]}

    def _pref(pref_id: str) -> dict:
        pref = sim.preferencias.get(pref_id)
        if not pref:
            raise HTTPException(404, "preferencia inexistente en el simulador")
        return pref

    @router.get("/checkout/{pref_id}", response_class=HTMLResponse, summary="Checkout falso (imita init_point)")
    def checkout(pref_id: str):
        pref = _pref(pref_id)
        filas = "".join(
            f"<tr><td>{html.escape(str(i['title']))}</td><td>{i['quantity']}</td><td>{i['unit_price']:.2f}</td></tr>"
            for i in pref["items"])
        total = sum(float(i["unit_price"]) * int(i["quantity"]) for i in pref["items"])
        botones = "".join(
            f'<button name="resultado" value="{r}">{t}</button>'
            for r, t in (("approved", "Pagar (aprobado)"), ("rejected", "Pagar (rechazado)"),
                         ("pending", "Pagar en efectivo (pendiente)")))
        return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Simulador de Mercado Pago</title>
<style>body{{font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;padding:0 16px}}
table{{width:100%;border-collapse:collapse}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left}}
button{{display:block;width:100%;margin:8px 0;padding:12px;font-size:16px;cursor:pointer}}
.aviso{{background:#fff3cd;padding:8px;border-radius:6px}}</style></head><body>
<p class="aviso">SIMULADOR LOCAL: no es Mercado Pago real y no se cobra nada.</p>
<h1>Pagar orden {html.escape(str(pref.get('external_reference')))}</h1>
<table><tr><th>Ítem</th><th>Cant.</th><th>Precio</th></tr>{filas}</table>
<p><b>Total: {total:.2f}</b></p>
<form method="post" action="/simulador/pagar/{html.escape(pref_id)}">{botones}</form></body></html>"""

    @router.post("/pagar/{pref_id}", summary="El comprador 'paga' desde el checkout falso")
    async def pagar_formulario(pref_id: str, request: Request, resultado: str = Form("approved")):
        pref = _pref(pref_id)
        try:
            pago = sim.pagar(pref_id, resultado)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if pref.get("notification_url"):
            await enviar_notificacion(request, str(pago["id"]), pref["notification_url"], "payment.created")
        # Igual que Mercado Pago: vuelve a la back_url con los datos del pago en la query.
        destino = {"approved": "success", "rejected": "failure"}.get(resultado, "pending")
        back = (pref.get("back_urls") or {}).get(destino) or "/"
        params = urlencode({"collection_id": pago["id"], "collection_status": resultado, "payment_id": pago["id"],
                            "status": resultado, "external_reference": pago["external_reference"],
                            "preference_id": pref_id})
        return RedirectResponse(f"{back}{'&' if '?' in back else '?'}{params}", status_code=303)

    @router.post("/pagos", summary="Crear un pago simulado por API (para scripts y pruebas)")
    async def pagar_api(pedido: PedidoPagoSimulado, request: Request):
        pref = _pref(pedido.preferencia_id)
        try:
            pago = sim.pagar(pedido.preferencia_id, pedido.resultado, pedido.monto)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        webhook = None
        if pedido.notificar and pref.get("notification_url"):
            webhook = await enviar_notificacion(request, str(pago["id"]), pref["notification_url"], "payment.created")
        return {"pago": pago, "webhook": webhook}

    @router.post("/notificar/{pago_id}", summary="Reenviar la notificación de un pago (nueva firma)")
    async def renotificar(pago_id: str, request: Request):
        pago = sim.pagos.get(pago_id)
        if not pago:
            raise HTTPException(404, "pago inexistente en el simulador")
        pref = _pref(pago["preference_id"])
        return await enviar_notificacion(request, pago_id, pref["notification_url"], "payment.updated")

    @router.get("/pagos/{pago_id}", summary="Ver un pago del simulador")
    def ver_pago(pago_id: str):
        pago = sim.pagos.get(pago_id)
        if not pago:
            raise HTTPException(404, "pago inexistente en el simulador")
        return pago

    return router
