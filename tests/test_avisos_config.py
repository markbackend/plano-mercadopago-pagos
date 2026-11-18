"""Avisos al dueño (webhook propio y SMTP falsos) y validación de la configuración."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.avisos import Avisador, formatear_monto
from app.config import Config, ErrorConfiguracion
from app.main import crear_app
from conftest import crear_orden, pagar

ORDEN = {"id": "abc", "total_centavos": 380000, "moneda": "ARS", "pago_id": "99",
         "comprador_email": "c@d.com", "items": [{"titulo": "Remera", "cantidad": 2, "precio_unitario_centavos": 150000}]}


class SMTPFalso:
    enviados: list = []

    def __init__(self, host, puerto, timeout=None):
        self.host, self.puerto = host, puerto
        self.llamadas = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.llamadas.append("starttls")

    def login(self, usuario, clave):
        self.llamadas.append(("login", usuario))

    def send_message(self, msg):
        SMTPFalso.enviados.append((self, msg))


def cfg(tmp_path, **kw) -> Config:
    return Config(modo="simulador", db_path=str(tmp_path / "a.db"), **kw).completar()


def test_formatear_monto():
    assert formatear_monto(380000, "ARS") == "ARS 3.800,00"
    assert formatear_monto(5, "USD") == "USD 0,05"
    assert formatear_monto(123456789, "ARS") == "ARS 1.234.567,89"


def test_aviso_por_webhook(tmp_path):
    recibidos = []
    transporte = httpx.MockTransport(lambda r: recibidos.append(r) or httpx.Response(200))
    av = Avisador(cfg(tmp_path, aviso_webhook_url="https://hooks.ejemplo.com/x"), transporte_http=transporte)
    assert av.pago_aprobado(ORDEN) == {"webhook": "ok"}
    cuerpo = json.loads(recibidos[0].content)
    assert cuerpo["evento"] == "pago_aprobado" and cuerpo["total"] == 3800.0
    assert "ARS 3.800,00" in cuerpo["texto"] and "2 x Remera" in cuerpo["texto"]


def test_aviso_por_webhook_que_falla_no_lanza(tmp_path):
    av = Avisador(cfg(tmp_path, aviso_webhook_url="https://hooks.ejemplo.com/x"),
                  transporte_http=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert av.pago_aprobado(ORDEN)["webhook"].startswith("error")


def test_aviso_por_email(tmp_path):
    SMTPFalso.enviados = []
    c = cfg(tmp_path, smtp_host="smtp.ejemplo.com", smtp_usuario="u", smtp_clave="k",
            smtp_remitente="tienda@ejemplo.com", aviso_email_para=["duenio@ejemplo.com", "socio@ejemplo.com"])
    assert Avisador(c, fabrica_smtp=SMTPFalso).pago_aprobado(ORDEN) == {"email": "ok"}
    smtp, msg = SMTPFalso.enviados[0]
    assert smtp.host == "smtp.ejemplo.com" and smtp.puerto == 587
    assert smtp.llamadas == ["starttls", ("login", "u")]
    assert msg["To"] == "duenio@ejemplo.com, socio@ejemplo.com"
    assert "abc" in msg["Subject"] and "ARS 3.800,00" in msg["Subject"]


def test_aviso_por_email_que_falla_no_lanza(tmp_path):
    def roto(*a, **k):
        raise OSError("conexión rechazada")
    c = cfg(tmp_path, smtp_host="smtp.x", smtp_remitente="a@b.c", aviso_email_para=["d@e.f"])
    assert "conexión rechazada" in Avisador(c, fabrica_smtp=roto).pago_aprobado(ORDEN)["email"]


def test_sin_canales_no_esta_activo(tmp_path):
    assert Avisador(cfg(tmp_path)).activo is False


def test_aviso_integrado_queda_en_el_historial(tmp_path):
    recibidos = []
    c = cfg(tmp_path, aviso_webhook_url="https://hooks.ejemplo.com/x")
    av = Avisador(c, transporte_http=httpx.MockTransport(lambda r: recibidos.append(r) or httpx.Response(204)))
    with TestClient(crear_app(c, avisador=av)) as api:
        orden = crear_orden(api)
        pagar(api, orden)
        eventos = [e["tipo"] for e in api.get(f"/ordenes/{orden['id']}").json()["eventos"]]
    assert len(recibidos) == 1
    assert eventos[-1] == "aviso_enviado"


# ---------------------------------------------------------------- configuración

def test_config_desde_entorno(monkeypatch):
    for k, v in {"MP_MODO": "sandbox", "MP_ACCESS_TOKEN": "TEST-abc", "MP_WEBHOOK_SECRET": "sec",
                 "BASE_URL_PUBLICA": "https://pagos.x.com/", "AVISO_EMAIL_PARA": "a@b.com, c@d.com",
                 "MP_FIRMA_TOLERANCIA_SEG": "600", "SMTP_HOST": "", "MONEDA": "ars"}.items():
        monkeypatch.setenv(k, v)
    c = Config.desde_entorno(cargar_dotenv=False)
    assert c.modo == "sandbox" and c.access_token == "TEST-abc"
    assert c.notification_url == "https://pagos.x.com/webhooks/mercadopago"
    assert c.aviso_email_para == ["a@b.com", "c@d.com"]
    assert c.firma_tolerancia_seg == 600 and c.moneda == "ARS"
    assert c.url_exito == "https://pagos.x.com/pago/resultado?estado=exito"


@pytest.mark.parametrize("kw,mensaje", [
    ({"modo": "otro"}, "MP_MODO"),
    ({"modo": "sandbox"}, "MP_ACCESS_TOKEN"),
    ({"modo": "produccion", "access_token": "APP_USR-1"}, "MP_WEBHOOK_SECRET"),
    ({"modo": "simulador", "smtp_host": "smtp.x"}, "SMTP_REMITENTE"),
])
def test_config_invalida(kw, mensaje):
    with pytest.raises(ErrorConfiguracion, match=mensaje):
        Config(**kw).completar()


def test_entero_invalido(monkeypatch):
    monkeypatch.setenv("SMTP_PUERTO", "abc")
    with pytest.raises(ErrorConfiguracion, match="SMTP_PUERTO"):
        Config.desde_entorno(cargar_dotenv=False)
