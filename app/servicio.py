"""Lógica de negocio: crear órdenes, procesar notificaciones y reembolsar.

Reglas importantes:
  - El estado del pago SIEMPRE se consulta a la API de Mercado Pago; nunca se confía en el cuerpo
    de la notificación (que podría ser falso o viejo).
  - Idempotencia: cada notificación se registra con una clave única. Si llega dos veces, la segunda
    no hace nada. Además el aviso al dueño solo se manda en la transición a "aprobada".
  - Si una orden ya está aprobada, un intento de pago distinto (por ej. una tarjeta rechazada antes)
    no la pisa.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from .avisos import Avisador
from .config import Config
from .db import BaseDatos, ahora_iso
from .mercadopago import ClienteMercadoPago, ErrorMercadoPago, estado_orden_desde_pago

log = logging.getLogger("pagos.servicio")

ESTADOS_ORDEN = ("pendiente", "aprobada", "rechazada", "reembolsada", "en_revision")


class OrdenNoEncontrada(Exception):
    pass


class OperacionInvalida(Exception):
    pass


def a_centavos(valor: Decimal | float | int | str) -> int:
    return int((Decimal(str(valor)) * 100).quantize(Decimal("1")))


def presentar_orden(orden: dict) -> dict:
    """Pasa la orden de la base (centavos) al formato de la API (montos con decimales)."""
    salida = {k: v for k, v in orden.items() if k not in ("total_centavos", "items")}
    salida["total"] = orden["total_centavos"] / 100
    if "items" in orden:
        salida["items"] = [
            {"titulo": i["titulo"], "cantidad": i["cantidad"], "precio_unitario": i["precio_unitario_centavos"] / 100}
            for i in orden["items"]
        ]
    return salida


class ServicioPagos:
    def __init__(self, config: Config, db: BaseDatos, mp: ClienteMercadoPago, avisador: Avisador):
        self.config = config
        self.db = db
        self.mp = mp
        self.avisador = avisador

    # ------------------------------------------------------------------ crear orden

    def armar_preferencia(self, orden_id: str, items: list[dict], comprador_email: str | None) -> dict:
        c = self.config
        pref: dict = {
            "items": [
                {
                    "id": f"{orden_id}-{n}",
                    "title": i["titulo"],
                    "quantity": i["cantidad"],
                    "unit_price": i["precio_unitario_centavos"] / 100,
                    "currency_id": c.moneda,
                }
                for n, i in enumerate(items, start=1)
            ],
            "external_reference": orden_id,
            "back_urls": {"success": c.url_exito, "failure": c.url_fallo, "pending": c.url_pendiente},
            "statement_descriptor": c.nombre_tienda[:22],
            "metadata": {"orden_id": orden_id},
        }
        # auto_return lleva al comprador de vuelta a la web solo; Mercado Pago lo rechaza con URLs locales.
        if c.modo == "simulador" or not any(x in c.url_exito for x in ("localhost", "127.0.0.1")):
            pref["auto_return"] = "approved"
        # Mercado Pago no puede llamar a localhost: en ese caso no mandamos notification_url
        # (se configura la URL en el panel o se usa un túnel tipo ngrok / cloudflared).
        if c.modo == "simulador" or not c.base_es_local:
            pref["notification_url"] = f"{c.notification_url}?source_news=webhooks"
        else:
            log.warning("BASE_URL_PUBLICA es local (%s): la preferencia sale sin notification_url", c.base_url_publica)
        if comprador_email:
            pref["payer"] = {"email": comprador_email}
        return pref

    def crear_orden(self, items: list[dict], comprador_email: str | None = None) -> dict:
        """items: [{"titulo", "cantidad", "precio_unitario"}]. Devuelve la orden con link_pago."""
        if not items:
            raise OperacionInvalida("la orden necesita al menos un ítem")
        normalizados = [
            {"titulo": i["titulo"].strip(), "cantidad": int(i["cantidad"]),
             "precio_unitario_centavos": a_centavos(i["precio_unitario"])}
            for i in items
        ]
        total = sum(i["cantidad"] * i["precio_unitario_centavos"] for i in normalizados)
        if total <= 0:
            raise OperacionInvalida("el total de la orden tiene que ser mayor a cero")

        orden_id = uuid.uuid4().hex[:16]
        preferencia = self.armar_preferencia(orden_id, normalizados, comprador_email)
        # Primero la preferencia: si Mercado Pago falla, no queda una orden huérfana en la base.
        resp = self.mp.crear_preferencia(preferencia, clave_idempotencia=f"pref-{orden_id}")
        if self.config.modo == "sandbox":
            link = resp.get("sandbox_init_point") or resp.get("init_point")
        else:
            link = resp.get("init_point")
        if not resp.get("id") or not link:
            raise ErrorMercadoPago("la respuesta de Mercado Pago no trae id o init_point", detalle=resp)

        self.db.insertar_orden(
            {"id": orden_id, "estado": "pendiente", "total_centavos": total, "moneda": self.config.moneda,
             "comprador_email": comprador_email, "creada": ahora_iso()},
            normalizados,
        )
        with self.db.transaccion() as c:
            self.db.actualizar_orden(c, orden_id, preferencia_id=resp["id"], link_pago=link)
            self.db.evento(c, orden_id, "preferencia_creada", {"preferencia_id": resp["id"]})
        log.info("Orden %s creada: total %s %s, preferencia %s", orden_id, total / 100, self.config.moneda, resp["id"])
        return self.ver_orden(orden_id)

    # ------------------------------------------------------------------ consultas

    def ver_orden(self, orden_id: str) -> dict:
        orden = self.db.obtener_orden(orden_id)
        if not orden:
            raise OrdenNoEncontrada(orden_id)
        return orden

    def listar_ordenes(self, estado: str | None = None, limite: int = 50, desde: int = 0) -> list[dict]:
        return self.db.listar_ordenes(estado, limite, desde)

    # ------------------------------------------------------------------ notificaciones

    def procesar_notificacion(self, tipo: str | None, data_id: str | None, clave: str) -> dict:
        """Procesa una notificación ya validada. Devuelve {"resultado": ..., ...}.

        Lanza ErrorMercadoPago si no se pudo consultar el pago y conviene que Mercado Pago reintente.
        """
        if tipo != "payment":
            log.info("Notificación de tipo %r ignorada", tipo)
            return {"resultado": "ignorada", "motivo": f"tipo {tipo!r} no manejado"}
        if not data_id:
            return {"resultado": "ignorada", "motivo": "sin data.id"}
        if self.db.notificacion_ya_procesada(clave):
            log.info("Notificación %s repetida: no se hace nada", clave)
            return {"resultado": "duplicada"}

        try:
            pago = self.mp.obtener_pago(data_id)
        except ErrorMercadoPago as exc:
            if exc.status == 404:
                # Pasa con las notificaciones de prueba del panel (data.id inventado). No reintentar.
                log.warning("El pago %s no existe en Mercado Pago", data_id)
                return {"resultado": "pago_no_encontrado"}
            raise

        pago_id = str(pago.get("id", data_id))
        status_mp = pago.get("status", "")
        orden_id = pago.get("external_reference") or (pago.get("metadata") or {}).get("orden_id")
        nuevo = estado_orden_desde_pago(status_mp)
        aviso = False

        with self.db.transaccion() as c:
            orden = self.db.obtener_orden(orden_id, c) if orden_id else None
            if not self.db.registrar_notificacion(c, clave, pago_id, orden["id"] if orden else None,
                                                  "procesando"):
                return {"resultado": "duplicada"}
            if not orden:
                log.warning("Pago %s con external_reference %r que no es de ninguna orden", pago_id, orden_id)
                c.execute("UPDATE notificaciones SET resultado = 'orden_desconocida' WHERE clave = ?", (clave,))
                return {"resultado": "orden_desconocida", "pago_id": pago_id}

            anterior = orden["estado"]
            otro_pago = orden["pago_id"] and orden["pago_id"] != pago_id
            if otro_pago and anterior in ("aprobada", "reembolsada", "en_revision") and nuevo != "aprobada":
                # Ej.: primero aprobaron una tarjeta y llega tarde el rechazo de un intento anterior.
                self.db.evento(c, orden["id"], "pago_ignorado", {"pago_id": pago_id, "status": status_mp})
                c.execute("UPDATE notificaciones SET resultado = 'ignorada' WHERE clave = ?", (clave,))
                log.info("Orden %s ya %s: se ignora el pago %s (%s)", orden["id"], anterior, pago_id, status_mp)
                return {"resultado": "ignorada", "orden_id": orden["id"], "estado": anterior}

            observacion = orden.get("observacion")
            if nuevo == "aprobada":
                pagado = a_centavos(pago.get("transaction_amount") or 0)
                moneda = pago.get("currency_id") or orden["moneda"]
                if pagado < orden["total_centavos"] or moneda != orden["moneda"]:
                    nuevo = "en_revision"
                    observacion = (f"Pago aprobado por {moneda} {pagado / 100} pero la orden es de "
                                   f"{orden['moneda']} {orden['total_centavos'] / 100}")
                    log.error("Orden %s: %s", orden["id"], observacion)

            self.db.actualizar_orden(c, orden["id"], estado=nuevo, pago_id=pago_id, estado_mp=status_mp,
                                     detalle_mp=pago.get("status_detail"), observacion=observacion)
            self.db.evento(c, orden["id"], "pago_actualizado",
                           {"pago_id": pago_id, "status": status_mp, "de": anterior, "a": nuevo})
            c.execute("UPDATE notificaciones SET resultado = ? WHERE clave = ?", (nuevo, clave))
            aviso = nuevo == "aprobada" and anterior != "aprobada"

        log.info("Orden %s: %s -> %s (pago %s, %s)", orden["id"], anterior, nuevo, pago_id, status_mp)
        if aviso and self.avisador.activo:
            orden_actual = self.ver_orden(orden["id"])
            resultado_avisos = self.avisador.pago_aprobado(orden_actual)
            with self.db.transaccion() as c:
                self.db.evento(c, orden["id"], "aviso_enviado", resultado_avisos)
        return {"resultado": "procesada", "orden_id": orden["id"], "estado": nuevo, "pago_id": pago_id}

    # ------------------------------------------------------------------ reembolso

    def reembolsar(self, orden_id: str) -> dict:
        orden = self.ver_orden(orden_id)
        if orden["estado"] == "reembolsada":
            raise OperacionInvalida("la orden ya está reembolsada")
        if orden["estado"] not in ("aprobada", "en_revision") or not orden["pago_id"]:
            raise OperacionInvalida(f"solo se reembolsan órdenes aprobadas (estado actual: {orden['estado']})")

        # La clave de idempotencia evita un doble reembolso si se repite el pedido.
        reembolso = self.mp.reembolsar_pago(orden["pago_id"], clave_idempotencia=f"reembolso-{orden_id}")
        with self.db.transaccion() as c:
            self.db.actualizar_orden(c, orden_id, estado="reembolsada", reembolso_id=str(reembolso.get("id", "")))
            self.db.evento(c, orden_id, "reembolso", {"reembolso_id": reembolso.get("id"),
                                                      "monto": reembolso.get("amount")})
        log.info("Orden %s reembolsada (pago %s, reembolso %s)", orden_id, orden["pago_id"], reembolso.get("id"))
        return self.ver_orden(orden_id)
