"""Validación de la firma x-signature de las notificaciones (webhooks) de Mercado Pago.

Mercado Pago manda dos encabezados:
  x-signature:  "ts=1704908010,v1=<hmac hex>"
  x-request-id: "<uuid>"

El HMAC-SHA256 (con la "clave secreta" de la sección Webhooks del panel) se calcula sobre el manifest:
  "id:<data.id>;request-id:<x-request-id>;ts:<ts>;"

donde data.id es el parámetro de la URL (?data.id=...). Si data.id es alfanumérico va en minúsculas.
Si falta algún valor, esa parte se omite del manifest (así lo indica la documentación oficial).
"""

from __future__ import annotations

import hashlib
import hmac
import time


class FirmaInvalida(Exception):
    """La notificación no trae una firma válida."""


def armar_manifest(data_id: str | None, request_id: str | None, ts: str | None) -> str:
    partes = []
    if data_id:
        partes.append(f"id:{data_id.lower() if not data_id.isdigit() else data_id};")
    if request_id:
        partes.append(f"request-id:{request_id};")
    if ts:
        partes.append(f"ts:{ts};")
    return "".join(partes)


def calcular_firma(secreto: str, manifest: str) -> str:
    return hmac.new(secreto.encode(), manifest.encode(), hashlib.sha256).hexdigest()


def armar_encabezado(secreto: str, data_id: str, request_id: str, ts: str | None = None) -> str:
    """Arma un x-signature como lo haría Mercado Pago (lo usa el simulador y las pruebas)."""
    ts = ts or str(int(time.time()))
    v1 = calcular_firma(secreto, armar_manifest(data_id, request_id, ts))
    return f"ts={ts},v1={v1}"


def leer_encabezado(x_signature: str) -> tuple[str, str]:
    """Devuelve (ts, v1) a partir de "ts=...,v1=..." (el orden de las partes puede variar)."""
    valores: dict[str, str] = {}
    for parte in x_signature.split(","):
        clave, _, valor = parte.strip().partition("=")
        if clave and valor:
            valores[clave.strip()] = valor.strip()
    ts, v1 = valores.get("ts"), valores.get("v1")
    if not ts or not v1:
        raise FirmaInvalida("x-signature mal formado: faltan ts o v1")
    return ts, v1


def validar_firma(
    secreto: str,
    x_signature: str | None,
    x_request_id: str | None,
    data_id: str | None,
    tolerancia_seg: int = 0,
    ahora: float | None = None,
) -> None:
    """Lanza FirmaInvalida si la firma no corresponde. No devuelve nada si está bien.

    tolerancia_seg > 0 rechaza firmas con un ts más viejo que eso (protección contra repetición).
    """
    if not x_signature:
        raise FirmaInvalida("falta el encabezado x-signature")
    ts, v1 = leer_encabezado(x_signature)

    if tolerancia_seg > 0:
        try:
            ts_num = float(ts)
        except ValueError as exc:
            raise FirmaInvalida("ts no es numérico") from exc
        if ts_num >= 1e11:  # algunos envíos usan milisegundos (en segundos recién llegaríamos en el año 5000)
            ts_num /= 1000
        ahora = time.time() if ahora is None else ahora
        if abs(ahora - ts_num) > tolerancia_seg:
            raise FirmaInvalida("la firma está vencida (ts fuera de la tolerancia)")

    esperada = calcular_firma(secreto, armar_manifest(data_id, x_request_id, ts))
    if not hmac.compare_digest(esperada, v1.lower()):
        raise FirmaInvalida("la firma no coincide")
