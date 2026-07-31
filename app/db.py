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
CREATE TABLE IF NOT EXISTS net_worth_history (
    day TEXT PRIMARY KEY,       -- YYYY-MM-DD
    krw REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE NOT NULL,
    sort_order INTEGER DEFAULT 0
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
        # migration: fixed KRW cost basis (for foreign holdings where FX matters)
        cols = [r[1] for r in c.execute("PRAGMA table_info(positions)")]
        if "cost_krw" not in cols:
            c.execute("ALTER TABLE positions ADD COLUMN cost_krw REAL")
        # seed the account list from any accounts already used by positions
        used = [r[0] for r in c.execute(
            "SELECT DISTINCT account FROM positions WHERE account IS NOT NULL AND account <> ''"
        )]
        for i, name in enumerate(used):
            c.execute(
                "INSERT OR IGNORE INTO accounts (name, sort_order) VALUES (?, ?)", (name, i)
            )


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


def record_net_worth(day: str, krw: float):
    """Upsert one snapshot per day (last visit of the day wins)."""
    with conn() as c:
        c.execute(
            "INSERT INTO net_worth_history (day, krw) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET krw=excluded.krw",
            (day, krw),
        )


def net_worth_series(limit_days: int = 120):
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT day, krw FROM net_worth_history ORDER BY day DESC LIMIT ?", (limit_days,)
        )]
    return list(reversed(rows))  # oldest -> newest


# --- accounts (user-managed portfolio groupings) ---
def list_accounts():
    """Account names in display order, each with its position count."""
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT a.name, a.sort_order, "
            "  (SELECT COUNT(*) FROM positions p WHERE p.account = a.name) AS n "
            "FROM accounts a ORDER BY a.sort_order, a.id"
        )]


def add_account(name: str):
    """Register an account. No-op if it already exists. Returns True if created."""
    name = (name or "").strip()
    if not name:
        return False
    with conn() as c:
        nxt = c.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM accounts").fetchone()[0]
        cur = c.execute(
            "INSERT OR IGNORE INTO accounts (name, sort_order) VALUES (?, ?)", (name, nxt)
        )
        return cur.rowcount > 0


def rename_account(old: str, new: str):
    """Rename an account and re-point every position under it. Returns error msg or None."""
    old = (old or "").strip()
    new = (new or "").strip()
    if not new:
        return "새 이름을 입력해 주세요."
    if new == old:
        return None
    with conn() as c:
        exists = c.execute("SELECT 1 FROM accounts WHERE name=?", (new,)).fetchone()
        if exists:
            return f"'{new}' 계좌가 이미 있습니다."
        c.execute("UPDATE accounts SET name=? WHERE name=?", (new, old))
        c.execute("UPDATE positions SET account=? WHERE account=?", (new, old))
    return None


def delete_account(name: str):
    """Delete an empty account. Returns error msg if it still holds positions."""
    name = (name or "").strip()
    with conn() as c:
        n = c.execute("SELECT COUNT(*) FROM positions WHERE account=?", (name,)).fetchone()[0]
        if n:
            return f"'{name}' 계좌에 종목이 {n}개 있습니다. 먼저 옮기거나 지워 주세요."
        c.execute("DELETE FROM accounts WHERE name=?", (name,))
    return None


def all_positions():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM positions ORDER BY account, id")]


def get_position(pid: int):
    with conn() as c:
        r = c.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


def upsert_position(data: dict, pid: int | None = None):
    fields = ("account", "name", "ticker", "market", "currency", "shares", "avg_cost", "manual_value_krw", "cost_krw")
    vals = [data.get(f) or None for f in fields]
    acc = (data.get("account") or "").strip()
    with conn() as c:
        if acc:  # keep the account list in sync with whatever positions use
            nxt = c.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM accounts").fetchone()[0]
            c.execute("INSERT OR IGNORE INTO accounts (name, sort_order) VALUES (?, ?)", (acc, nxt))
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
