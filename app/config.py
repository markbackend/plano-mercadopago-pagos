"""Configuración leída de variables de entorno (o de un archivo .env).

Todas las claves salen del entorno: nunca se escriben en el código.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

log = logging.getLogger("pagos.config")

MODOS_VALIDOS = ("simulador", "sandbox", "produccion")


class ErrorConfiguracion(Exception):
    """La configuración está incompleta o es inconsistente."""


def _bool(valor: str | None, defecto: bool = False) -> bool:
    if valor is None or valor.strip() == "":
        return defecto
    return valor.strip().lower() in ("1", "true", "si", "sí", "yes", "on")


def _int(nombre: str, valor: str | None, defecto: int) -> int:
    if valor is None or valor.strip() == "":
        return defecto
    try:
        return int(valor)
    except ValueError as exc:
        raise ErrorConfiguracion(f"{nombre} debe ser un número entero (vino {valor!r})") from exc


@dataclass
class Config:
    # Modo de trabajo: "simulador" (sin cuenta, todo local), "sandbox" (credenciales de prueba) o "produccion".
    modo: str = "simulador"
    access_token: str = ""
    webhook_secret: str = ""
    api_base: str = "https://api.mercadopago.com"
    api_timeout_seg: float = 15.0

    # URL pública de esta API (para notification_url y back_urls). Ej: https://pagos.miweb.com
    base_url_publica: str = "http://127.0.0.1:8000"
    url_exito: str = ""
    url_fallo: str = ""
    url_pendiente: str = ""
    moneda: str = "ARS"
    nombre_tienda: str = "Mi tienda"  # aparece en el resumen de la tarjeta (statement_descriptor)

    db_path: str = "datos/pagos.db"
    admin_api_key: str = ""  # si se define, /ordenes exige el encabezado X-API-Key
    firma_tolerancia_seg: int = 0  # 0 = no controla la antigüedad del ts de la firma

    # Avisos al dueño cuando se aprueba un pago (todos opcionales).
    aviso_webhook_url: str = ""
    smtp_host: str = ""
    smtp_puerto: int = 587
    smtp_usuario: str = ""
    smtp_clave: str = ""
    smtp_starttls: bool = True
    smtp_remitente: str = ""
    aviso_email_para: list[str] = field(default_factory=list)

    log_nivel: str = "INFO"

    @classmethod
    def desde_entorno(cls, cargar_dotenv: bool = True) -> "Config":
        if cargar_dotenv:
            load_dotenv()
        e = os.environ.get
        base = (e("BASE_URL_PUBLICA") or "http://127.0.0.1:8000").rstrip("/")
        cfg = cls(
            modo=(e("MP_MODO") or "simulador").strip().lower(),
            access_token=(e("MP_ACCESS_TOKEN") or "").strip(),
            webhook_secret=(e("MP_WEBHOOK_SECRET") or "").strip(),
            api_base=(e("MP_API_BASE") or "https://api.mercadopago.com").rstrip("/"),
            api_timeout_seg=float(e("MP_TIMEOUT_SEG") or 15),
            base_url_publica=base,
            url_exito=e("URL_EXITO") or "",
            url_fallo=e("URL_FALLO") or "",
            url_pendiente=e("URL_PENDIENTE") or "",
            moneda=(e("MONEDA") or "ARS").strip().upper(),
            nombre_tienda=e("NOMBRE_TIENDA") or "Mi tienda",
            db_path=e("DB_PATH") or "datos/pagos.db",
            admin_api_key=(e("ADMIN_API_KEY") or "").strip(),
            firma_tolerancia_seg=_int("MP_FIRMA_TOLERANCIA_SEG", e("MP_FIRMA_TOLERANCIA_SEG"), 0),
            aviso_webhook_url=(e("AVISO_WEBHOOK_URL") or "").strip(),
            smtp_host=(e("SMTP_HOST") or "").strip(),
            smtp_puerto=_int("SMTP_PUERTO", e("SMTP_PUERTO"), 587),
            smtp_usuario=e("SMTP_USUARIO") or "",
            smtp_clave=e("SMTP_CLAVE") or "",
            smtp_starttls=_bool(e("SMTP_STARTTLS"), True),
            smtp_remitente=e("SMTP_REMITENTE") or "",
            aviso_email_para=[x.strip() for x in (e("AVISO_EMAIL_PARA") or "").split(",") if x.strip()],
            log_nivel=(e("LOG_NIVEL") or "INFO").upper(),
        )
        cfg.completar()
        return cfg

    def completar(self) -> "Config":
        """Rellena valores derivados y valida. Devuelve self para encadenar."""
        if self.modo not in MODOS_VALIDOS:
            raise ErrorConfiguracion(f"MP_MODO debe ser uno de {MODOS_VALIDOS} (vino {self.modo!r})")
        base = self.base_url_publica.rstrip("/")
        self.base_url_publica = base
        self.url_exito = self.url_exito or f"{base}/pago/resultado?estado=exito"
        self.url_fallo = self.url_fallo or f"{base}/pago/resultado?estado=fallo"
        self.url_pendiente = self.url_pendiente or f"{base}/pago/resultado?estado=pendiente"

        if self.modo == "simulador":
            # El simulador firma con este secreto; no es una clave real.
            self.access_token = self.access_token or "SIMULADOR-TOKEN"
            self.webhook_secret = self.webhook_secret or "secreto-del-simulador"
            # Nombre ficticio para que los logs no muestren la URL real: el simulador nunca sale a la red.
            if self.api_base == "https://api.mercadopago.com":
                self.api_base = "http://simulador-mercadopago.local"
        else:
            if not self.access_token:
                raise ErrorConfiguracion(f"En modo {self.modo} falta MP_ACCESS_TOKEN")
            if self.modo == "produccion" and not self.webhook_secret:
                raise ErrorConfiguracion("En producción MP_WEBHOOK_SECRET es obligatorio (valida la firma)")
            if self.modo == "produccion" and self.access_token.startswith("TEST-"):
                log.warning("MP_MODO=produccion pero el token empieza con TEST-: parece de prueba")
            if not self.webhook_secret:
                log.warning("Sin MP_WEBHOOK_SECRET: las notificaciones NO se validan (solo aceptable en pruebas)")
        if self.smtp_host and not (self.smtp_remitente and self.aviso_email_para):
            raise ErrorConfiguracion("Con SMTP_HOST hay que definir SMTP_REMITENTE y AVISO_EMAIL_PARA")
        return self

    @property
    def notification_url(self) -> str:
        return f"{self.base_url_publica}/webhooks/mercadopago"

    @property
    def base_es_local(self) -> bool:
        return any(x in self.base_url_publica for x in ("localhost", "127.0.0.1", "0.0.0.0"))
