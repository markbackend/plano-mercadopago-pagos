"""Webhook: firma, idempotencia, casos raros y concurrencia."""

import threading

from conftest import crear_orden, notificacion, pagar


def test_misma_notificacion_dos_veces_no_duplica(api, avisador):
    orden = crear_orden(api)
    pago = pagar(api, orden, notificar=False)["pago"]
    ruta, headers, cuerpo = notificacion(api, pago["id"], "payment.created")

    primera = api.post(ruta, headers=headers, json=cuerpo)
    db = api.app.state.db
    eventos = db.contar("eventos")
    segunda = api.post(ruta, headers=headers, json=cuerpo)

    assert primera.json()["resultado"] == "procesada"
    assert segunda.status_code == 200 and segunda.json() == {"resultado": "duplicada"}
    assert db.contar("eventos") == eventos
    assert db.contar("notificaciones") == 1
    assert len(avisador.enviados) == 1


def test_notificacion_nueva_del_mismo_pago_no_repite_el_aviso(api, avisador):
    orden = crear_orden(api)
    pago = pagar(api, orden)["pago"]
    ruta, headers, cuerpo = notificacion(api, pago["id"])
    assert api.post(ruta, headers=headers, json=cuerpo).json()["estado"] == "aprobada"
    assert len(avisador.enviados) == 1


def test_firma_invalida_no_toca_nada(api):
    orden = crear_orden(api)
    pago = pagar(api, orden, notificar=False)["pago"]
    ruta, headers, cuerpo = notificacion(api, pago["id"])
    headers["x-signature"] = headers["x-signature"].replace("v1=", "v1=00")
    r = api.post(ruta, headers=headers, json=cuerpo)
    assert r.status_code == 401
    assert api.get(f"/ordenes/{orden['id']}").json()["estado"] == "pendiente"
    assert api.app.state.db.contar("notificaciones") == 0


def test_sin_firma_se_rechaza(api):
    r = api.post("/webhooks/mercadopago?data.id=1&type=payment", json={"type": "payment", "data": {"id": "1"}})
    assert r.status_code == 401


def test_firma_de_otro_pago_no_sirve(api):
    """Alguien reusa una firma válida cambiando el data.id de la URL."""
    orden = crear_orden(api)
    pago = pagar(api, orden, notificar=False)["pago"]
    ruta, headers, cuerpo = notificacion(api, pago["id"])
    ruta = ruta.replace(f"data.id={pago['id']}", "data.id=999")
    assert api.post(ruta, headers=headers, json=cuerpo).status_code == 401


def test_otros_tipos_se_ignoran(api):
    from app.firma import armar_encabezado
    secreto = api.app.state.config.webhook_secret
    headers = {"x-signature": armar_encabezado(secreto, "5", "r1"), "x-request-id": "r1"}
    r = api.post("/webhooks/mercadopago?data.id=5&type=merchant_order", headers=headers, json={"id": 3})
    assert r.status_code == 200 and r.json()["resultado"] == "ignorada"


def test_pago_inexistente_responde_200_para_que_no_reintente(api):
    """Las notificaciones de prueba del panel de Mercado Pago traen un data.id inventado."""
    from app.firma import armar_encabezado
    secreto = api.app.state.config.webhook_secret
    headers = {"x-signature": armar_encabezado(secreto, "123456", "r2"), "x-request-id": "r2"}
    r = api.post("/webhooks/mercadopago?data.id=123456&type=payment", headers=headers,
                 json={"id": 4, "type": "payment", "data": {"id": "123456"}})
    assert r.status_code == 200 and r.json()["resultado"] == "pago_no_encontrado"


def test_pago_de_una_orden_desconocida(api, sim):
    orden = crear_orden(api)
    pago = sim.pagar(orden["preferencia_id"], "approved")
    pago["external_reference"] = "otra-tienda"
    ruta, headers, cuerpo = notificacion(api, pago["id"])
    assert api.post(ruta, headers=headers, json=cuerpo).json()["resultado"] == "orden_desconocida"
    assert api.get(f"/ordenes/{orden['id']}").json()["estado"] == "pendiente"


def test_rechazo_tardio_de_otro_intento_no_pisa_la_aprobacion(api, sim, avisador):
    orden = crear_orden(api)
    rechazado = sim.pagar(orden["preferencia_id"], "rejected")
    pagar(api, orden, "approved")
    # La notificación del intento rechazado llega tarde.
    ruta, headers, cuerpo = notificacion(api, rechazado["id"])
    r = api.post(ruta, headers=headers, json=cuerpo).json()
    assert r["resultado"] == "ignorada"
    assert api.get(f"/ordenes/{orden['id']}").json()["estado"] == "aprobada"
    assert len(avisador.enviados) == 1


def test_monto_menor_queda_en_revision_y_sin_aviso(api, avisador):
    orden = crear_orden(api)
    pagar(api, orden, "approved", monto=10.0)
    vista = api.get(f"/ordenes/{orden['id']}").json()
    assert vista["estado"] == "en_revision"
    assert "10.0" in vista["observacion"]
    assert avisador.enviados == []


def test_reembolso_hecho_desde_el_panel_llega_por_webhook(api, sim):
    orden = crear_orden(api)
    pago = pagar(api, orden)["pago"]
    sim.pagos[str(pago["id"])]["status"] = "refunded"
    ruta, headers, cuerpo = notificacion(api, pago["id"])
    assert api.post(ruta, headers=headers, json=cuerpo).json()["estado"] == "reembolsada"


def test_formato_viejo_ipn_con_topic(api, sim):
    """IPN viejo: ?id=..&topic=payment sin cuerpo. Sin firma -> se rechaza (solo aceptamos webhooks firmados)."""
    r = api.post("/webhooks/mercadopago?id=1&topic=payment")
    assert r.status_code == 401


def test_cuerpo_invalido_no_rompe(api):
    r = api.post("/webhooks/mercadopago?data.id=1&type=payment", content=b"{no es json",
                 headers={"content-type": "application/json"})
    assert r.status_code == 401  # sin firma, pero sin error 500


def test_concurrencia_misma_notificacion_en_paralelo(api, avisador):
    orden = crear_orden(api)
    pago = pagar(api, orden, notificar=False)["pago"]
    servicio = api.app.state.servicio
    resultados = []

    def procesar():
        resultados.append(servicio.procesar_notificacion("payment", str(pago["id"]), "notif:igual")["resultado"])

    hilos = [threading.Thread(target=procesar) for _ in range(8)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert sorted(resultados) == ["duplicada"] * 7 + ["procesada"]
    assert len(avisador.enviados) == 1
