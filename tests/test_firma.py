import hashlib
import hmac

import pytest

from app.firma import FirmaInvalida, armar_encabezado, armar_manifest, leer_encabezado, validar_firma

SECRETO = "mi-clave-secreta"


def hmac_manual(texto: str) -> str:
    return hmac.new(SECRETO.encode(), texto.encode(), hashlib.sha256).hexdigest()


def test_manifest_con_todas_las_partes():
    assert armar_manifest("123456", "abc-1", "1704908010") == "id:123456;request-id:abc-1;ts:1704908010;"


def test_manifest_id_alfanumerico_va_en_minusculas():
    assert armar_manifest("ABC123", "r", "1") == "id:abc123;request-id:r;ts:1;"


def test_manifest_omite_partes_faltantes():
    assert armar_manifest(None, "r", "1") == "request-id:r;ts:1;"
    assert armar_manifest("9", None, "1") == "id:9;ts:1;"


def test_firma_valida_calculada_a_mano():
    v1 = hmac_manual("id:123456;request-id:req-1;ts:1704908010;")
    validar_firma(SECRETO, f"ts=1704908010,v1={v1}", "req-1", "123456")


def test_partes_del_encabezado_en_otro_orden_y_con_espacios():
    v1 = hmac_manual("id:1;request-id:r;ts:5;")
    validar_firma(SECRETO, f" v1={v1} , ts=5 ", "r", "1")
    assert leer_encabezado(f"v1={v1},ts=5") == ("5", v1)


@pytest.mark.parametrize("cambio", ["data_id", "request_id", "secreto", "firma"])
def test_cualquier_cambio_invalida_la_firma(cambio):
    datos = {"data_id": "123", "request_id": "req", "secreto": SECRETO}
    encabezado = armar_encabezado(SECRETO, "123", "req", "100")
    if cambio == "firma":
        encabezado = encabezado[:-1] + ("0" if encabezado[-1] != "0" else "1")
    else:
        datos[cambio] = datos[cambio] + "x"
    with pytest.raises(FirmaInvalida):
        validar_firma(datos["secreto"], encabezado, datos["request_id"], datos["data_id"])


@pytest.mark.parametrize("encabezado", [None, "", "ts=1", "v1=abc", "basura"])
def test_encabezado_faltante_o_mal_formado(encabezado):
    with pytest.raises(FirmaInvalida):
        validar_firma(SECRETO, encabezado, "req", "1")


def test_tolerancia_rechaza_firmas_viejas():
    enc = armar_encabezado(SECRETO, "1", "r", "1000")
    validar_firma(SECRETO, enc, "r", "1", tolerancia_seg=300, ahora=1200)
    with pytest.raises(FirmaInvalida, match="vencida"):
        validar_firma(SECRETO, enc, "r", "1", tolerancia_seg=300, ahora=2000)


def test_tolerancia_acepta_ts_en_milisegundos():
    enc = armar_encabezado(SECRETO, "1", "r", "1000000000000")  # 1e12 ms = 1e9 s
    validar_firma(SECRETO, enc, "r", "1", tolerancia_seg=60, ahora=1_000_000_010)


def test_sin_tolerancia_no_mira_el_ts():
    enc = armar_encabezado(SECRETO, "1", "r", "1")
    validar_firma(SECRETO, enc, "r", "1", tolerancia_seg=0, ahora=9e9)
