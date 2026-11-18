"""Demo de 1 minuto, sin cuenta ni internet: .venv/Scripts/python.exe demo.py

Recorre el flujo completo contra el simulador local de Mercado Pago:
crear orden -> link de pago -> pago aprobado -> notificación firmada -> orden aprobada
-> notificación repetida (no duplica) -> reembolso. Usa una base temporal que se borra al final.
"""

from __future__ import annotations

import json
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)  # aviso interno de starlette.testclient

from fastapi.testclient import TestClient

from app.config import Config
from app.main import crear_app


def paso(n: int, texto: str) -> None:
    print(f"\n=== {n}. {texto}")


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        config = Config(modo="simulador", db_path=str(Path(tmp) / "demo.db"), log_nivel="WARNING").completar()
        with TestClient(crear_app(config)) as api:
            paso(1, "Salud de la API")
            print(api.get("/salud").json())

            paso(2, "Crear una orden (2 remeras a $1500 + envío $800)")
            r = api.post("/ordenes", json={
                "items": [{"titulo": "Remera talle M", "cantidad": 2, "precio_unitario": "1500.00"},
                          {"titulo": "Envío", "cantidad": 1, "precio_unitario": "800"}],
                "comprador_email": "comprador@prueba.com"})
            orden = r.json()
            print(f"Orden {orden['id']} - total {orden['total']} - estado {orden['estado']}")
            print(f"Link de pago (init_point): {orden['link_pago']}")

            paso(3, "El comprador paga y el simulador manda la notificación FIRMADA al webhook")
            r = api.post("/simulador/pagos", json={"preferencia_id": orden["preferencia_id"], "resultado": "approved"})
            pago = r.json()
            print(f"Pago {pago['pago']['id']} {pago['pago']['status']} -> webhook {pago['webhook']['status']}: "
                  f"{pago['webhook']['respuesta']}")
            print("Estado de la orden:", api.get(f"/ordenes/{orden['id']}").json()["estado"])

            paso(4, "Llega otra notificación del mismo pago y Mercado Pago la reintenta (idempotencia)")
            sim = api.app.state.simulador
            url, headers, cuerpo = sim.armar_notificacion(str(pago["pago"]["id"]), config.webhook_secret,
                                                          config.notification_url)
            ruta = url.replace(config.base_url_publica, "")
            for intento in (1, 2):
                print(f"Envío {intento}:", api.post(ruta, headers=headers, json=cuerpo).json())

            paso(5, "Una notificación con firma falsa se rechaza")
            malos = {**headers, "x-signature": headers["x-signature"][:-4] + "beef"}
            r = api.post(ruta, headers=malos, json={**cuerpo, "id": 1})
            print(r.status_code, r.json())

            paso(6, "Reembolso total")
            r = api.post(f"/ordenes/{orden['id']}/reembolso")
            print("Estado:", r.json()["estado"], "- reembolso", r.json()["reembolso_id"])

            paso(7, "Historial de la orden")
            for e in api.get(f"/ordenes/{orden['id']}").json()["eventos"]:
                print(f"  {e['fecha']}  {e['tipo']:<20} {json.loads(e['detalle'])}")

            paso(8, "Listado de órdenes")
            for o in api.get("/ordenes").json():
                print(f"  {o['id']}  {o['estado']:<12} {o['moneda']} {o['total']}")
    print("\nListo. Para probarlo en el navegador: .venv/Scripts/python.exe -m app  y abrir http://127.0.0.1:8000")


if __name__ == "__main__":
    main()
