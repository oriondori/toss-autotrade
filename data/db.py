"""SQLite 저장소 — 시그널·주문·일별손익·로그."""
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "autotrade.db"
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT DEFAULT '',
    side TEXT NOT NULL,           -- BUY / SELL / HOLD
    strategy TEXT DEFAULT '',
    reason TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT DEFAULT '',
    side TEXT NOT NULL,           -- BUY / SELL
    order_type TEXT DEFAULT '',   -- LIMIT / MARKET
    price REAL,
    qty REAL,
    status TEXT DEFAULT 'PENDING',-- PENDING / FILLED / CANCELED / REJECTED
    fill_price REAL,
    reason TEXT DEFAULT '',
    client_order_id TEXT DEFAULT '',
    mode TEXT DEFAULT 'paper'     -- paper / live
);
CREATE TABLE IF NOT EXISTS daily_pnl (
    date TEXT PRIMARY KEY,
    total_asset REAL,
    realized_pnl REAL DEFAULT 0,
    fees REAL DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,          -- INFO / WARN / ERROR
    category TEXT DEFAULT 'SYS',  -- AUTH/LOOP/SIGNAL/RISK/ORDER/FILL/RATE/SYS
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,       -- 1m / 1d
    ts TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, interval, ts)
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _lock, _conn() as c:
        c.executescript(_SCHEMA)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 쓰기 ----------
def log(level: str, category: str, message: str) -> None:
    with _lock, _conn() as c:
        c.execute("INSERT INTO logs (ts, level, category, message) VALUES (?,?,?,?)",
                  (now(), level, category, message))


def add_signal(symbol: str, side: str, strategy: str = "", reason: str = "",
               name: str = "") -> None:
    with _lock, _conn() as c:
        c.execute("INSERT INTO signals (ts,symbol,name,side,strategy,reason) VALUES (?,?,?,?,?,?)",
                  (now(), symbol, name, side, strategy, reason))


def add_order(**kw) -> int:
    cols = ("symbol", "name", "side", "order_type", "price", "qty",
            "status", "fill_price", "reason", "client_order_id", "mode")
    vals = [now()] + [kw.get(k) for k in cols]
    with _lock, _conn() as c:
        cur = c.execute(
            f"INSERT INTO orders (ts,{','.join(cols)}) VALUES ({','.join('?' * (len(cols) + 1))})",
            vals)
        return cur.lastrowid


def update_order(order_id: int, **kw) -> None:
    sets = ", ".join(f"{k}=?" for k in kw)
    with _lock, _conn() as c:
        c.execute(f"UPDATE orders SET {sets} WHERE id=?", [*kw.values(), order_id])


def upsert_daily_pnl(date: str, **kw) -> None:
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO daily_pnl (date) VALUES (?)", (date,))
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE daily_pnl SET {sets} WHERE date=?", [*kw.values(), date])


# ---------- 읽기 ----------
def recent(table: str, limit: int = 50) -> list[dict]:
    assert table in ("signals", "orders", "logs")
    with _lock, _conn() as c:
        rows = c.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


def pnl_history(days: int = 30) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM daily_pnl ORDER BY date DESC LIMIT ?", (days,))
        return sorted([dict(r) for r in rows], key=lambda r: r["date"])
