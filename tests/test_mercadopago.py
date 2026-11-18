"""Cliente HTTP y armado de la preferencia, con un transporte httpx falso que registra los pedidos."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import crear_app
from app.mercadopago import ClienteMercadoPago, ErrorMercadoPago, estado_orden_desde_pago
from conftest import ITEMS


class MPFalso:
    """Transporte que responde lo que le digamos y guarda cada pedido."""

    def __init__(self, respuestas=None):
        self.pedidos: list[httpx.Request] = []
        self.respuestas = respuestas or {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.pedidos.append(req)
        clave = f"{req.method} {req.url.path}"
        r = self.respuestas.get(clave)
        if isinstance(r, Exception):
            raise r
        if r is None:
            return httpx.Response(404, json={"message": "no configurado"})
        return r

    def cliente(self) -> ClienteMercadoPago:
        return ClienteMercadoPago("TEST-123", "https://api.mercadopago.com", transporte=httpx.MockTransport(self))


def test_manda_token_e_idempotencia():
    mp = MPFalso({"POST /checkout/preferences": httpx.Response(201, json={"id": "p1", "init_point": "x"})})
    mp.cliente().crear_preferencia({"items": []}, clave_idempotencia="pref-1")
    req = mp.pedidos[0]
    assert req.headers["authorization"] == "Bearer TEST-123"
    assert req.headers["x-idempotency-key"] == "pref-1"
    assert str(req.url) == "https://api.mercadopago.com/checkout/preferences"


def test_reembolso_total_cuerpo_vacio_con_clave():
    mp = MPFalso({"POST /v1/payments/55/refunds": httpx.Response(201, json={"id": 9, "status": "approved"})})
    assert mp.cliente().reembolsar_pago("55", "reembolso-x")["id"] == 9
    assert json.loads(mp.pedidos[0].content) == {}
    assert mp.pedidos[0].headers["x-idempotency-key"] == "reembolso-x"


@pytest.mark.parametrize("status,temporal", [(500, True), (503, True), (429, True), (400, False), (401, False)])
def test_errores_http(status, temporal):
    mp = MPFalso({"GET /v1/payments/1": httpx.Response(status, json={"message": "algo"})})
    with pytest.raises(ErrorMercadoPago) as e:
        mp.cliente().obtener_pago("1")
    assert e.value.status == status
    assert e.value.es_temporal is temporal
    assert "algo" in str(e.value)


def test_error_de_red_es_temporal():
    mp = MPFalso({"GET /v1/payments/1": httpx.ConnectError("sin red")})
    with pytest.raises(ErrorMercadoPago) as e:
        mp.cliente().obtener_pago("1")
    assert e.value.status is None and e.value.es_temporal


def test_respuesta_no_json():
    mp = MPFalso({"GET /v1/payments/1": httpx.Response(502, text="<html>Bad gateway</html>")})
    with pytest.raises(ErrorMercadoPago):
        mp.cliente().obtener_pago("1")


@pytest.mark.parametrize("mp_status,esperado", [
    ("approved", "aprobada"), ("pending", "pendiente"), ("in_process", "pendiente"),
    ("rejected", "rechazada"), ("cancelled", "rechazada"), ("refunded", "reembolsada"),
    ("charged_back", "reembolsada"), ("desconocido", "pendiente")])
def test_mapeo_de_estados(mp_status, esperado):
    assert estado_orden_desde_pago(mp_status) == esperado


# ---------------------------------------------------------------- app en modo sandbox con transporte falso

def app_sandbox(tmp_path, mp: MPFalso, **cfg):
    config = Config(modo="sandbox", access_token="TEST-123", webhook_secret="s", db_path=str(tmp_path / "s.db"),
                    **cfg).completar()
    return TestClient(crear_app(config, transporte_mp=httpx.MockTransport(mp)))


def test_preferencia_completa_en_sandbox(tmp_path):
    mp = MPFalso({"POST /checkout/preferences": httpx.Response(201, json={
        "id": "pref-1", "init_point": "https://www.mercadopago.com.ar/checkout/v1/redirect?pref_id=pref-1",
        "sandbox_init_point": "https://sandbox.mercadopago.com.ar/checkout/v1/redirect?pref_id=pref-1"})})
    with app_sandbox(tmp_path, mp, base_url_publica="https://pagos.mitienda.com") as api:
        r = api.post("/ordenes", json={"items": ITEMS, "comprador_email": "a@b.com"})
    assert r.status_code == 201, r.text
    orden = r.json()
    assert orden["link_pago"].startswith("https://sandbox.mercadopago.com.ar")
    assert orden["total"] == 3800.0

    pref = json.loads(mp.pedidos[0].content)
    assert pref["external_reference"] == orden["id"]
    assert pref["notification_url"] == "https://pagos.mitienda.com/webhooks/mercadopago?source_news=webhooks"
    assert pref["back_urls"]["success"] == "https://pagos.mitienda.com/pago/resultado?estado=exito"
    assert pref["auto_return"] == "approved"
    assert pref["payer"] == {"email": "a@b.com"}
    assert pref["items"][0] == {"id": f"{orden['id']}-1", "title": "Remera", "quantity": 2,
                                "unit_price": 1500.0, "currency_id": "ARS"}


def test_con_url_local_no_manda_notification_url_ni_auto_return(tmp_path):
    mp = MPFalso({"POST /checkout/preferences": httpx.Response(201, json={"id": "p", "init_point": "https://x"})})
    with app_sandbox(tmp_path, mp, base_url_publica="http://localhost:8000") as api:
        assert api.post("/ordenes", json={"items": ITEMS}).status_code == 201
    pref = json.loads(mp.pedidos[0].content)
    assert "notification_url" not in pref and "auto_return" not in pref


def test_si_mercadopago_falla_no_queda_orden(tmp_path):
    mp = MPFalso({"POST /checkout/preferences": httpx.Response(401, json={"message": "invalid access token"})})
    with app_sandbox(tmp_path, mp) as api:
        r = api.post("/ordenes", json={"items": ITEMS})
        assert r.status_code == 502
        assert "invalid access token" in r.json()["detail"]
        assert api.get("/ordenes").json() == []


def test_respuesta_sin_init_point_es_error(tmp_path):
    mp = MPFalso({"POST /checkout/preferences": httpx.Response(201, json={"id": "p"})})
    with app_sandbox(tmp_path, mp) as api:
        assert api.post("/ordenes", json={"items": ITEMS}).status_code == 502
        assert api.get("/ordenes").json() == []


def test_webhook_cuando_la_api_esta_caida_pide_reintento(tmp_path):
    from app.firma import armar_encabezado
    mp = MPFalso({"GET /v1/payments/77": httpx.Response(500, json={"message": "internal"})})
    with app_sandbox(tmp_path, mp) as api:
        headers = {"x-signature": armar_encabezado("s", "77", "req-1"), "x-request-id": "req-1"}
        r = api.post("/webhooks/mercadopago?data.id=77&type=payment", headers=headers,
                     json={"id": 1, "type": "payment", "data": {"id": "77"}})
        assert r.status_code == 503
        assert api.app.state.db.contar("notificaciones") == 0  # el reintento se va a procesar
