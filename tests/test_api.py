"""Endpoints de la API y flujo completo contra el simulador local."""

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import crear_app
from conftest import ITEMS, crear_orden, pagar


def test_salud(api):
    assert api.get("/salud").json() == {"estado": "ok", "modo": "simulador", "base_datos": "ok"}


def test_crear_orden_devuelve_link(api, sim):
    orden = crear_orden(api, comprador_email="cliente@correo.com")
    assert orden["estado"] == "pendiente"
    assert orden["total"] == 3800.0
    assert orden["link_pago"].endswith(f"/simulador/checkout/{orden['preferencia_id']}")
    assert orden["items"][1] == {"titulo": "Envío", "cantidad": 1, "precio_unitario": 800.0}
    assert orden["comprador_email"] == "cliente@correo.com"
    assert sim.preferencias[orden["preferencia_id"]]["external_reference"] == orden["id"]


def test_montos_con_decimales_sin_errores_de_redondeo(api):
    orden = crear_orden(api, items=[{"titulo": "x", "cantidad": 3, "precio_unitario": "0.10"}])
    assert orden["total"] == 0.3


@pytest.mark.parametrize("cuerpo", [
    {"items": []},
    {"items": [{"titulo": "x", "cantidad": 1, "precio_unitario": "0"}]},
    {"items": [{"titulo": "x", "cantidad": 0, "precio_unitario": "10"}]},
    {"items": [{"titulo": "", "cantidad": 1, "precio_unitario": "10"}]},
    {"items": [{"titulo": "x", "cantidad": 1, "precio_unitario": "10.123"}]},
    {"items": [{"titulo": "x", "cantidad": 1, "precio_unitario": "10"}], "comprador_email": "no-es-email"},
])
def test_validaciones_de_entrada(api, cuerpo):
    assert api.post("/ordenes", json=cuerpo).status_code == 422


def test_ver_orden_inexistente(api):
    r = api.get("/ordenes/nada")
    assert r.status_code == 404 and "nada" in r.json()["detail"]


def test_listar_y_filtrar(api):
    a = crear_orden(api)
    b = crear_orden(api)
    pagar(api, b)
    todas = api.get("/ordenes").json()
    assert {o["id"] for o in todas} == {a["id"], b["id"]}
    assert [o["id"] for o in api.get("/ordenes?estado=aprobada").json()] == [b["id"]]
    assert [o["id"] for o in api.get("/ordenes?estado=pendiente").json()] == [a["id"]]
    assert len(api.get("/ordenes?limite=1").json()) == 1
    assert api.get("/ordenes?estado=inventado").status_code == 422


def test_flujo_pago_aprobado_y_reembolso(api, avisador):
    orden = crear_orden(api)
    resp = pagar(api, orden, "approved")
    assert resp["webhook"]["status"] == 200
    assert resp["webhook"]["respuesta"]["estado"] == "aprobada"

    vista = api.get(f"/ordenes/{orden['id']}").json()
    assert vista["estado"] == "aprobada"
    assert vista["pago_id"] == str(resp["pago"]["id"])
    assert vista["estado_mp"] == "approved"
    assert len(avisador.enviados) == 1

    r = api.post(f"/ordenes/{orden['id']}/reembolso")
    assert r.status_code == 200 and r.json()["estado"] == "reembolsada"
    assert r.json()["reembolso_id"]
    assert api.app.state.simulador.pagos[str(resp["pago"]["id"])]["status"] == "refunded"
    # Segundo reembolso: no se permite.
    assert api.post(f"/ordenes/{orden['id']}/reembolso").status_code == 409


def test_pago_rechazado(api, avisador):
    orden = crear_orden(api)
    pagar(api, orden, "rejected")
    assert api.get(f"/ordenes/{orden['id']}").json()["estado"] == "rechazada"
    assert avisador.enviados == []


def test_rechazado_y_despues_aprobado_con_otra_tarjeta(api):
    orden = crear_orden(api)
    pagar(api, orden, "rejected")
    pagar(api, orden, "approved")
    assert api.get(f"/ordenes/{orden['id']}").json()["estado"] == "aprobada"


def test_pendiente_y_despues_acreditado(api, sim, avisador):
    from conftest import notificacion
    orden = crear_orden(api)
    pago = pagar(api, orden, "pending")["pago"]
    assert api.get(f"/ordenes/{orden['id']}").json()["estado"] == "pendiente"
    # El comprador paga en efectivo: Mercado Pago actualiza el pago y vuelve a notificar.
    sim.pagos[str(pago["id"])]["status"] = "approved"
    ruta, headers, cuerpo = notificacion(api, pago["id"])
    assert api.post(ruta, headers=headers, json=cuerpo).json()["estado"] == "aprobada"
    assert len(avisador.enviados) == 1


def test_no_se_reembolsa_una_orden_pendiente(api):
    orden = crear_orden(api)
    r = api.post(f"/ordenes/{orden['id']}/reembolso")
    assert r.status_code == 409 and "aprobadas" in r.json()["detail"]


def test_checkout_simulado_en_el_navegador(api):
    orden = crear_orden(api)
    pagina = api.get(orden["link_pago"].replace("http://127.0.0.1:8000", ""))
    assert pagina.status_code == 200 and "SIMULADOR" in pagina.text and "Remera" in pagina.text

    r = api.post(f"/simulador/pagar/{orden['preferencia_id']}", data={"resultado": "approved"},
                 follow_redirects=False)
    assert r.status_code == 303
    destino = r.headers["location"]
    assert destino.startswith("http://127.0.0.1:8000/pago/resultado?estado=exito&")
    assert f"external_reference={orden['id']}" in destino and "status=approved" in destino
    assert api.get(f"/ordenes/{orden['id']}").json()["estado"] == "aprobada"
    assert "aprobado" in api.get("/pago/resultado?estado=exito").text


def test_pagina_de_inicio_del_simulador(api):
    assert "Crear orden de prueba" in api.get("/").text


def test_api_key(tmp_path):
    config = Config(modo="simulador", db_path=str(tmp_path / "k.db"), admin_api_key="clave-admin").completar()
    with TestClient(crear_app(config)) as api:
        assert api.post("/ordenes", json={"items": ITEMS}).status_code == 401
        assert api.get("/ordenes", headers={"X-API-Key": "otra"}).status_code == 401
        r = api.post("/ordenes", json={"items": ITEMS}, headers={"X-API-Key": "clave-admin"})
        assert r.status_code == 201
        assert api.get("/salud").status_code == 200  # salud y webhook quedan públicos
        # El webhook no exige API key (lo protege la firma).
        assert api.post(f"/simulador/pagos", json={"preferencia_id": r.json()["preferencia_id"]}).json()[
            "webhook"]["status"] == 200


def test_los_datos_persisten_al_reiniciar(config):
    with TestClient(crear_app(config)) as api:
        orden = crear_orden(api)
    with TestClient(crear_app(config)) as api:
        assert api.get(f"/ordenes/{orden['id']}").json()["total"] == 3800.0


def test_sin_simulador_en_otros_modos(tmp_path):
    import httpx
    config = Config(modo="sandbox", access_token="TEST-1", db_path=str(tmp_path / "x.db")).completar()
    with TestClient(crear_app(config, transporte_mp=httpx.MockTransport(lambda r: httpx.Response(500)))) as api:
        assert api.get("/simulador/checkout/x").status_code == 404
        assert api.get("/").status_code == 404
