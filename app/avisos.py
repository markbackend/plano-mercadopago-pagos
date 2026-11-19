"""Avisos al dueño de la tienda cuando se aprueba un pago.

Dos canales opcionales e independientes:
  - Webhook propio: POST JSON a AVISO_WEBHOOK_URL (sirve para Slack, Discord, n8n, Zapier, Make, etc.).
  - Email por SMTP (Gmail con contraseña de aplicación, Brevo, Mailgun, el hosting del cliente...).

Un aviso que falla NUNCA rompe el procesamiento del pago: se registra en el log y se sigue.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Callable

import httpx

from .config import Config

log = logging.getLogger("pagos.avisos")


def formatear_monto(centavos: int, moneda: str) -> str:
    entero, cent = divmod(centavos, 100)
    return f"{moneda} {entero:,}".replace(",", ".") + f",{cent:02d}"


class Avisador:
    def __init__(
        self,
        config: Config,
        transporte_http: httpx.BaseTransport | None = None,
        fabrica_smtp: Callable[..., smtplib.SMTP] = smtplib.SMTP,
    ):
        self.config = config
        self._transporte = transporte_http
        self._fabrica_smtp = fabrica_smtp

    @property
    def activo(self) -> bool:
        return bool(self.config.aviso_webhook_url or self.config.smtp_host)

    def pago_aprobado(self, orden: dict) -> dict[str, str]:
        """Manda los avisos configurados. Devuelve {canal: "ok" | "error: ..."}."""
        resultado: dict[str, str] = {}
        if self.config.aviso_webhook_url:
            resultado["webhook"] = self._intentar(self._por_webhook, orden)
        if self.config.smtp_host:
            resultado["email"] = self._intentar(self._por_email, orden)
        return resultado

    def _intentar(self, funcion: Callable[[dict], None], orden: dict) -> str:
        try:
            funcion(orden)
            return "ok"
        except Exception as exc:  # noqa: BLE001 - un aviso no debe tirar abajo el webhook
            log.error("No se pudo mandar el aviso (%s) de la orden %s: %s", funcion.__name__, orden["id"], exc)
            return f"error: {exc}"

    @staticmethod
    def _revisar(orden: dict) -> bool:
        return orden.get("estado") == "en_revision" and bool(orden.get("observacion"))

    def _texto(self, orden: dict) -> str:
        lineas = [f"REVISAR: {orden['observacion']}", ""] if self._revisar(orden) else []
        lineas += [
            f"Pago aprobado en {self.config.nombre_tienda}",
            f"Orden: {orden['id']}",
            f"Total: {formatear_monto(orden['total_centavos'], orden['moneda'])}",
            f"Pago de Mercado Pago: {orden.get('pago_id')}",
        ]
        if orden.get("comprador_email"):
            lineas.append(f"Comprador: {orden['comprador_email']}")
        for i in orden.get("items", []):
            lineas.append(f"- {i['cantidad']} x {i['titulo']}")
        return "\n".join(lineas)

    def _por_webhook(self, orden: dict) -> None:
        cuerpo = {
            "evento": "pago_aprobado",
            "orden_id": orden["id"],
            "total": orden["total_centavos"] / 100,
            "moneda": orden["moneda"],
            "pago_id": orden.get("pago_id"),
            "comprador_email": orden.get("comprador_email"),
            "texto": self._texto(orden),  # Slack/Discord muestran este campo tal cual
            "content": self._texto(orden),
        }
        with httpx.Client(timeout=10, transport=self._transporte) as http:
            resp = http.post(self.config.aviso_webhook_url, json=cuerpo)
            resp.raise_for_status()
        log.info("Aviso por webhook enviado (orden %s)", orden["id"])

    def _por_email(self, orden: dict) -> None:
        c = self.config
        msg = EmailMessage()
        prefijo = "REVISAR (doble cobro o monto distinto)" if self._revisar(orden) else "Pago aprobado"
        msg["Subject"] = f"{prefijo} - orden {orden['id']} - {formatear_monto(orden['total_centavos'], orden['moneda'])}"
        msg["From"] = c.smtp_remitente
        msg["To"] = ", ".join(c.aviso_email_para)
        msg.set_content(self._texto(orden))
        with self._fabrica_smtp(c.smtp_host, c.smtp_puerto, timeout=15) as smtp:
            if c.smtp_starttls:
                smtp.starttls()
            if c.smtp_usuario:
                smtp.login(c.smtp_usuario, c.smtp_clave)
            smtp.send_message(msg)
        log.info("Aviso por email enviado a %s (orden %s)", msg["To"], orden["id"])
