"""SQLite storage for portfolio positions. Stdlib only, no ORM."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("ASSET_DB", os.path.join(os.path.dirname(__file__), "..", "data", "asset.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account          TEXT NOT NULL,
    name             TEXT NOT NULL,
    ticker           TEXT,
    market           TEXT NOT NULL,      -- US | KR | MANUAL
    currency         TEXT NOT NULL,      -- USD | KRW
    shares           REAL,
    avg_cost         REAL,
    manual_value_krw REAL,               -- MANUAL: 평가액 / 그 외: 시세조회 실패 시 폴백
    updated_at       TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS change_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT DEFAULT (datetime('now','localtime')),
    action  TEXT NOT NULL,      -- 추가 | 수정 | 삭제
    account TEXT,
    name    TEXT,
    detail  TEXT
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)


def get_setting(key: str):
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None


def set_setting(key: str, value: str):
    with conn() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def add_log(action: str, account: str, name: str, detail: str):
    with conn() as c:
        c.execute(
            "INSERT INTO change_log (action, account, name, detail) VALUES (?, ?, ?, ?)",
            (action, account, name, detail),
        )


def recent_logs(limit: int = 100):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM change_log ORDER BY id DESC LIMIT ?", (limit,)
        )]


def all_positions():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM positions ORDER BY account, id")]


def get_position(pid: int):
    with conn() as c:
        r = c.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


def upsert_position(data: dict, pid: int | None = None):
    fields = ("account", "name", "ticker", "market", "currency", "shares", "avg_cost", "manual_value_krw")
    vals = [data.get(f) or None for f in fields]
    with conn() as c:
        if pid:
            c.execute(
                f"UPDATE positions SET {', '.join(f+'=?' for f in fields)}, "
                "updated_at=datetime('now','localtime') WHERE id=?",
                (*vals, pid),
            )
        else:
            c.execute(
                f"INSERT INTO positions ({', '.join(fields)}) VALUES ({', '.join('?'*len(fields))})",
                vals,
            )


def delete_position(pid: int):
    with conn() as c:
        c.execute("DELETE FROM positions WHERE id=?", (pid,))


def count():
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
