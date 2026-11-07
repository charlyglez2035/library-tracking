"""Persistencia propia del rastreo.

SQLite vive solo en este servicio: guarda la asociacion codigo <-> pedido real y no
duplica datos comerciales, que siguen en PostgreSQL detras de pedidos y pagos.
"""
import re
import sqlite3
import time
from datetime import datetime, timezone

CODE_PATTERN = re.compile(r"^LIB-MTY-\d{4}-\d{6,}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS shipment (
    tracking_code TEXT PRIMARY KEY,
    order_id INTEGER NOT NULL UNIQUE,
    started_at REAL NOT NULL,
    synced_status TEXT NOT NULL DEFAULT 'paid'
)
"""


class TrackingStore:
    def __init__(self, path):
        self.path = str(path)
        with self._connect() as conn:
            conn.execute(SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def build_code(order_id, year=None):
        year = year or datetime.now(timezone.utc).year
        return f"LIB-MTY-{year}-{order_id:06d}"

    def get_or_create(self, order_id):
        """Idempotente: un pedido conserva su codigo y el inicio de su simulacion."""
        code = self.build_code(order_id)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO shipment (tracking_code, order_id, started_at) VALUES (?, ?, ?)",
                (code, order_id, time.time()),
            )
            return dict(conn.execute(
                "SELECT * FROM shipment WHERE order_id = ?", (order_id,)
            ).fetchone())

    def find_by_code(self, code):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM shipment WHERE tracking_code = ?", (code,)
            ).fetchone()
        return dict(row) if row else None

    def find_by_orders(self, order_ids):
        if not order_ids:
            return []
        placeholders = ", ".join("?" for _ in order_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM shipment WHERE order_id IN ({placeholders})", list(order_ids)
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_sync(self, code, previous, new_status):
        """Reserva una transicion para que solo un hilo la aplique en pedidos."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE shipment SET synced_status = ? WHERE tracking_code = ? AND synced_status = ?",
                (new_status, code, previous),
            )
            return cursor.rowcount == 1
