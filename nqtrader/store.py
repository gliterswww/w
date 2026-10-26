"""Persistent storage (SQLite, WAL mode) shared by the engine, research jobs and dashboard.

The engine and the dashboard run as separate processes and talk only through this database:
the dashboard writes control flags (emergency stop, alerts) and the engine reads them every bar.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, level TEXT NOT NULL, component TEXT NOT NULL,
    message TEXT NOT NULL, data TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    client_id TEXT PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT, side INTEGER, qty INTEGER,
    kind TEXT, price REAL, status TEXT, broker_id TEXT, parent TEXT, filled_price REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, day TEXT, strategy TEXT, symbol TEXT, side INTEGER, qty INTEGER,
    entry_time TEXT, exit_time TEXT, entry REAL, exit REAL, stop REAL, target REAL,
    reason TEXT, pnl_usd REAL, r_multiple REAL, mode TEXT
);
CREATE TABLE IF NOT EXISTS bars (
    ts TEXT PRIMARY KEY, open REAL, high REAL, low REAL, close REAL, volume REAL, vwap REAL
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY, balance REAL, equity REAL, unrealized REAL, day_pnl REAL, drawdown REAL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, created TEXT, price REAL, direction TEXT, note TEXT,
    status TEXT DEFAULT 'active', triggered_at TEXT
);
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY, created TEXT, kind TEXT, git_sha TEXT, data_hash TEXT, data_label TEXT,
    config TEXT, summary TEXT, report_path TEXT
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    version INTEGER PRIMARY KEY, created TEXT, strategy TEXT, params TEXT, status TEXT,
    experiment_id TEXT, notes TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        """One long-lived connection per thread (the dashboard serves requests from a pool)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")  # durable in WAL mode; no fsync per commit
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def execute(self, sql: str, args: tuple = ()) -> int:
        with self._conn() as c:
            return c.execute(sql, args).lastrowid

    # ---- key/value control state ---------------------------------------------

    def get(self, key: str, default=None):
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value) -> None:
        self.execute("INSERT INTO kv(key,value,updated) VALUES(?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                     (key, json.dumps(value, default=str), utcnow()))

    # ---- logging ---------------------------------------------------------------

    def log(self, level: str, component: str, message: str, ts: str | None = None, **data) -> None:
        self.execute("INSERT INTO events(ts,level,component,message,data) VALUES(?,?,?,?,?)",
                     (ts or utcnow(), level, component, message, json.dumps(data, default=str) if data else None))

    def events(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ---- orders (client_id is the idempotency key) -----------------------------

    def order_exists(self, client_id: str) -> bool:
        return bool(self.query("SELECT 1 FROM orders WHERE client_id=?", (client_id,)))

    def upsert_order(self, **o) -> None:
        cols = ",".join(o)
        self.execute(f"INSERT INTO orders({cols}) VALUES({','.join('?' * len(o))}) "
                     f"ON CONFLICT(client_id) DO UPDATE SET " + ",".join(f"{k}=excluded.{k}" for k in o if k != "client_id"),
                     tuple(o.values()))
