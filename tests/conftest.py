"""Fixtures compartidas. Nada sale a internet: Mercado Pago es el simulador o un transporte httpx falso."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import crear_app

ITEMS = [{"titulo": "Remera", "cantidad": 2, "precio_unitario": "1500.00"},
         {"titulo": "Envío", "cantidad": 1, "precio_unitario": "800"}]


class AvisadorFalso:
    """Registra los avisos en vez de mandarlos."""

    activo = True

    def __init__(self):
        self.enviados: list[dict] = []

    def pago_aprobado(self, orden: dict) -> dict:
        self.enviados.append(orden)
        return {"falso": "ok"}


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(modo="simulador", db_path=str(tmp_path / "pagos.db"), log_nivel="DEBUG").completar()


@pytest.fixture
def avisador() -> AvisadorFalso:
    return AvisadorFalso()


@pytest.fixture
def api(config, avisador):
    with TestClient(crear_app(config, avisador=avisador)) as cliente:
        yield cliente


@pytest.fixture
def sim(api):
    return api.app.state.simulador


def crear_orden(api, items=None, **extra) -> dict:
    r = api.post("/ordenes", json={"items": items or ITEMS, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def pagar(api, orden: dict, resultado: str = "approved", **extra) -> dict:
    r = api.post("/simulador/pagos", json={"preferencia_id": orden["preferencia_id"], "resultado": resultado, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def notificacion(api, pago_id, accion: str = "payment.updated"):
    """Arma una notificación firmada (url relativa, headers, cuerpo) igual a las de Mercado Pago."""
    cfg = api.app.state.config
    url, headers, cuerpo = api.app.state.simulador.armar_notificacion(
        str(pago_id), cfg.webhook_secret, cfg.notification_url, accion)
    return url.replace(cfg.base_url_publica, ""), headers, cuerpo
