"""Base de datos SQLite: órdenes, ítems, notificaciones procesadas e historial de eventos.

Los montos se guardan en centavos (enteros) para no arrastrar errores de punto flotante.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

ESQUEMA = """
CREATE TABLE IF NOT EXISTS ordenes (
    id              TEXT PRIMARY KEY,
    estado          TEXT NOT NULL,
    total_centavos  INTEGER NOT NULL,
    moneda          TEXT NOT NULL,
    comprador_email TEXT,
    preferencia_id  TEXT,
    link_pago       TEXT,
    pago_id         TEXT,
    estado_mp       TEXT,
    detalle_mp      TEXT,
    reembolso_id    TEXT,
    observacion     TEXT,
    creada          TEXT NOT NULL,
    actualizada     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    orden_id                TEXT NOT NULL REFERENCES ordenes(id),
    titulo                  TEXT NOT NULL,
    cantidad                INTEGER NOT NULL,
    precio_unitario_centavos INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notificaciones (
    clave      TEXT PRIMARY KEY,
    pago_id    TEXT,
    orden_id   TEXT,
    resultado  TEXT,
    recibida   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eventos (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    orden_id TEXT NOT NULL,
    tipo     TEXT NOT NULL,
    detalle  TEXT,
    fecha    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ordenes_estado ON ordenes(estado);
CREATE INDEX IF NOT EXISTS idx_eventos_orden ON eventos(orden_id);
"""


def ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BaseDatos:
    def __init__(self, ruta: str):
        self.ruta = ruta
        if ruta != ":memory:":
            Path(ruta).parent.mkdir(parents=True, exist_ok=True)
        # Una sola conexión protegida con un lock: suficiente para el volumen de una tienda chica/mediana.
        self._con = sqlite3.connect(ruta, check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA foreign_keys = ON")
        if ruta != ":memory:":
            self._con.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._con.executescript(ESQUEMA)

    def cerrar(self) -> None:
        self._con.close()

    @contextmanager
    def transaccion(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                yield self._con
            except BaseException:
                self._con.execute("ROLLBACK")
                raise
            else:
                self._con.execute("COMMIT")

    # ---------- órdenes ----------

    def insertar_orden(self, orden: dict, items: list[dict]) -> None:
        with self.transaccion() as c:
            c.execute(
                """INSERT INTO ordenes (id, estado, total_centavos, moneda, comprador_email, creada, actualizada)
                   VALUES (:id, :estado, :total_centavos, :moneda, :comprador_email, :creada, :creada)""",
                orden,
            )
            c.executemany(
                """INSERT INTO items (orden_id, titulo, cantidad, precio_unitario_centavos)
                   VALUES (?, ?, ?, ?)""",
                [(orden["id"], i["titulo"], i["cantidad"], i["precio_unitario_centavos"]) for i in items],
            )
            self._evento(c, orden["id"], "orden_creada", {"total_centavos": orden["total_centavos"]})

    def actualizar_orden(self, c: sqlite3.Connection, orden_id: str, **campos) -> None:
        campos["actualizada"] = ahora_iso()
        asignaciones = ", ".join(f"{k} = :{k}" for k in campos)
        c.execute(f"UPDATE ordenes SET {asignaciones} WHERE id = :_id", {**campos, "_id": orden_id})

    def obtener_orden(self, orden_id: str, c: sqlite3.Connection | None = None) -> dict | None:
        con = c or self._con
        with self._lock:
            fila = con.execute("SELECT * FROM ordenes WHERE id = ?", (orden_id,)).fetchone()
            if not fila:
                return None
            orden = dict(fila)
            orden["items"] = [dict(i) for i in con.execute(
                "SELECT titulo, cantidad, precio_unitario_centavos FROM items WHERE orden_id = ? ORDER BY rowid",
                (orden_id,))]
            orden["eventos"] = [dict(e) for e in con.execute(
                "SELECT tipo, detalle, fecha FROM eventos WHERE orden_id = ? ORDER BY id", (orden_id,))]
        return orden

    def listar_ordenes(self, estado: str | None, limite: int, desde: int) -> list[dict]:
        sql = "SELECT * FROM ordenes"
        params: list = []
        if estado:
            sql += " WHERE estado = ?"
            params.append(estado)
        sql += " ORDER BY creada DESC, rowid DESC LIMIT ? OFFSET ?"
        params += [limite, desde]
        with self._lock:
            return [dict(f) for f in self._con.execute(sql, params)]

    # ---------- notificaciones / eventos ----------

    def notificacion_ya_procesada(self, clave: str) -> bool:
        with self._lock:
            return self._con.execute("SELECT 1 FROM notificaciones WHERE clave = ?", (clave,)).fetchone() is not None

    def registrar_notificacion(self, c: sqlite3.Connection, clave: str, pago_id: str,
                               orden_id: str | None, resultado: str) -> bool:
        """Devuelve False si la clave ya existía (otra petición la procesó primero)."""
        cur = c.execute(
            "INSERT OR IGNORE INTO notificaciones (clave, pago_id, orden_id, resultado, recibida) VALUES (?, ?, ?, ?, ?)",
            (clave, pago_id, orden_id, resultado, ahora_iso()),
        )
        return cur.rowcount == 1

    def _evento(self, c: sqlite3.Connection, orden_id: str, tipo: str, detalle: dict | None = None) -> None:
        c.execute("INSERT INTO eventos (orden_id, tipo, detalle, fecha) VALUES (?, ?, ?, ?)",
                  (orden_id, tipo, json.dumps(detalle or {}, ensure_ascii=False), ahora_iso()))

    def evento(self, c: sqlite3.Connection, orden_id: str, tipo: str, detalle: dict | None = None) -> None:
        self._evento(c, orden_id, tipo, detalle)

    def contar(self, tabla: str) -> int:
        assert tabla in ("ordenes", "items", "notificaciones", "eventos")
        with self._lock:
            return self._con.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]

    def ping(self) -> bool:
        with self._lock:
            return self._con.execute("SELECT 1").fetchone()[0] == 1
