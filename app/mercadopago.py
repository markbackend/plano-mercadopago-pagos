"""Cliente mínimo de la API REST de Mercado Pago, con httpx (sin SDK).

Endpoints usados:
  POST /checkout/preferences          -> crea la preferencia de Checkout Pro (link de pago)
  GET  /v1/payments/{id}              -> consulta un pago (fuente de verdad del estado)
  POST /v1/payments/{id}/refunds      -> reembolso total (cuerpo vacío)

El transporte de httpx se puede inyectar: así las pruebas y el simulador no salen a internet.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("pagos.mercadopago")


class ErrorMercadoPago(Exception):
    """Error al hablar con la API de Mercado Pago."""

    def __init__(self, mensaje: str, status: int | None = None, detalle: Any = None):
        super().__init__(mensaje)
        self.status = status
        self.detalle = detalle

    @property
    def es_temporal(self) -> bool:
        """True si vale la pena reintentar (red caída, 429 o 5xx)."""
        return self.status is None or self.status == 429 or self.status >= 500


class ClienteMercadoPago:
    def __init__(
        self,
        access_token: str,
        api_base: str = "https://api.mercadopago.com",
        timeout: float = 15.0,
        transporte: httpx.BaseTransport | None = None,
    ):
        self._http = httpx.Client(
            base_url=api_base,
            timeout=timeout,
            transport=transporte,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "plano-mercadopago-pagos/1.0",
            },
        )

    def cerrar(self) -> None:
        self._http.close()

    def _pedir(self, metodo: str, ruta: str, **kwargs: Any) -> dict:
        try:
            resp = self._http.request(metodo, ruta, **kwargs)
        except httpx.HTTPError as exc:
            log.error("Sin respuesta de Mercado Pago en %s %s: %s", metodo, ruta, exc)
            raise ErrorMercadoPago(f"no se pudo conectar con Mercado Pago: {exc}") from exc

        try:
            datos = resp.json() if resp.content else {}
        except ValueError:
            datos = {"texto": resp.text[:500]}

        if resp.status_code >= 400:
            mensaje = datos.get("message") or datos.get("error") or resp.reason_phrase
            log.error("Mercado Pago respondió %s en %s %s: %s", resp.status_code, metodo, ruta, mensaje)
            raise ErrorMercadoPago(f"Mercado Pago respondió {resp.status_code}: {mensaje}",
                                   status=resp.status_code, detalle=datos)
        return datos

    def crear_preferencia(self, preferencia: dict, clave_idempotencia: str | None = None) -> dict:
        headers = {"X-Idempotency-Key": clave_idempotencia} if clave_idempotencia else {}
        return self._pedir("POST", "/checkout/preferences", json=preferencia, headers=headers)

    def obtener_pago(self, pago_id: str) -> dict:
        return self._pedir("GET", f"/v1/payments/{pago_id}")

    def reembolsar_pago(self, pago_id: str, clave_idempotencia: str) -> dict:
        # Sin "amount" en el cuerpo = reembolso total.
        return self._pedir("POST", f"/v1/payments/{pago_id}/refunds", json={},
                           headers={"X-Idempotency-Key": clave_idempotencia})


# Traducción de los estados de pago de Mercado Pago a los estados de nuestra orden.
ESTADOS = {
    "approved": "aprobada",
    "authorized": "pendiente",
    "pending": "pendiente",
    "in_process": "pendiente",
    "in_mediation": "pendiente",
    "rejected": "rechazada",
    "cancelled": "rechazada",
    "refunded": "reembolsada",
    "charged_back": "reembolsada",
}


def estado_orden_desde_pago(status_mp: str) -> str:
    return ESTADOS.get(status_mp, "pendiente")
