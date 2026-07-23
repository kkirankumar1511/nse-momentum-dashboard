"""
SQLite-backed storage for live-trading state -- consolidates what used to
be four separate ad hoc files (cache/live_position_state.json,
cache/fund_state.json, cache/equity_log.csv, cache/rebalance_proposal.pkl)
into one queryable local database, free and dependency-free (sqlite3 is
Python stdlib).

Kept local-only and gitignored, same as everything else under cache/ --
this holds real account/position/fund data, never meant to leave the
machine.

cache/live_rebalance_log.txt stays separate and unchanged -- it serves a
different purpose (a plain-text tail-able trail for a headless scheduled
run with no console).
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3

import pandas as pd

DB_PATH = os.path.join("cache", "state.db")
_LEGACY_EQUITY_LOG = os.path.join("cache", "equity_log.csv")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    qty INTEGER NOT NULL,
    highest_close REAL NOT NULL,
    current_stop REAL NOT NULL,
    gtt_trigger_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    closed_date TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stop_update_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES positions(id),
    date TEXT NOT NULL,
    old_stop REAL NOT NULL,
    new_stop REAL NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fund_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital REAL NOT NULL,
    captured_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_log (
    date TEXT PRIMARY KEY,
    value REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rebalance_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_time TEXT NOT NULL,
    open_slots INTEGER,
    status TEXT NOT NULL,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS rebalance_sells (
    run_id INTEGER REFERENCES rebalance_runs(id),
    symbol TEXT, qty INTEGER, avg_price REAL, reason TEXT
);

CREATE TABLE IF NOT EXISTS rebalance_buys (
    run_id INTEGER REFERENCES rebalance_runs(id),
    symbol TEXT, qty INTEGER, price REAL, stop REAL, score REAL,
    fundamental_score REAL, fundamental_rubric TEXT
);

CREATE TABLE IF NOT EXISTS rebalance_stop_updates (
    run_id INTEGER REFERENCES rebalance_runs(id),
    symbol TEXT, qty INTEGER, current_stop REAL, recommended_stop REAL,
    gtt_trigger_id INTEGER
);
"""


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    """Row-factory'd connection; ensures schema exists (idempotent CREATE
    TABLE IF NOT EXISTS) on every call -- no separate init step to forget.

    db_path resolves DB_PATH at CALL time, not import time -- every other
    function in this module calls get_conn() with no argument, so this is
    what actually lets tests redirect state_db.DB_PATH to a throwaway file
    and have it take effect everywhere. A bare `db_path: str = DB_PATH`
    default would bind the value once at function-definition time and
    silently ignore a later `state_db.DB_PATH = ...` reassignment -- a real
    bug caught during verification (a test run wrote into the live
    cache/state.db instead of its intended throwaway file before this
    fix)."""
    db_path = db_path or DB_PATH
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    _migrate_equity_log_once(conn)
    return conn


def _migrate_equity_log_once(conn: sqlite3.Connection) -> None:
    """One-time carry-over of cache/equity_log.csv's existing rows -- not
    an ongoing dual-write, just preserves history from before this
    migration. No-ops once equity_log already has rows."""
    existing = conn.execute("SELECT COUNT(*) FROM equity_log").fetchone()[0]
    if existing or not os.path.exists(_LEGACY_EQUITY_LOG):
        return
    try:
        legacy = pd.read_csv(_LEGACY_EQUITY_LOG)
    except Exception:
        return
    for _, row in legacy.iterrows():
        conn.execute("INSERT OR IGNORE INTO equity_log (date, value) VALUES (?, ?)",
                    (row["date"], float(row["value"])))
    conn.commit()


# ---------------------------------------------------------------------------
# Positions (trailing-stop tracking)
# ---------------------------------------------------------------------------

def record_new_position(symbol: str, entry_price: float, qty: int,
                        stop: float, gtt_trigger_id: int | None) -> None:
    """Call right after a buy (+ GTT, if placed) succeeds -- seeds this
    symbol's trailing-stop bookkeeping. gtt_trigger_id=None if the GTT
    placement failed or was skipped."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO positions (symbol, entry_date, entry_price, qty, "
        "highest_close, current_stop, gtt_trigger_id, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
        (symbol, dt.date.today().isoformat(), entry_price, qty,
         entry_price, stop, gtt_trigger_id))
    conn.commit()
    conn.close()


def reconciled_positions(held_symbols: set[str]) -> dict[str, dict]:
    """Marks any 'open' row whose symbol isn't in held_symbols as 'closed'
    (closed_date=today) instead of deleting -- a GTT can close a position
    without any of this app's code running, so every read reconciles
    against real Kite holdings first; history is preserved rather than
    silently dropped. Returns the remaining open positions, keyed by
    symbol, same dict shape the old JSON version returned."""
    conn = get_conn()
    open_rows = conn.execute(
        "SELECT * FROM positions WHERE status = 'open'").fetchall()
    today = dt.date.today().isoformat()
    for row in open_rows:
        if row["symbol"] not in held_symbols:
            conn.execute(
                "UPDATE positions SET status = 'closed', closed_date = ? "
                "WHERE id = ?", (today, row["id"]))
    conn.commit()
    remaining = conn.execute(
        "SELECT * FROM positions WHERE status = 'open'").fetchall()
    conn.close()
    return {r["symbol"]: dict(r) for r in remaining}


def update_position_stop(symbol: str, highest_close: float, new_stop: float,
                         applied: bool = False) -> None:
    """Updates the open position's highest_close/current_stop and appends
    a stop_update_log row -- an audit trail the old JSON version had no
    way to keep, since it only ever stored the latest value."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM positions WHERE symbol = ? AND status = 'open'",
        (symbol,)).fetchone()
    if row is None:
        conn.close()
        return
    if new_stop > row["current_stop"]:
        conn.execute(
            "INSERT INTO stop_update_log (position_id, date, old_stop, "
            "new_stop, applied) VALUES (?, ?, ?, ?, ?)",
            (row["id"], dt.date.today().isoformat(), row["current_stop"],
             new_stop, int(applied)))
    conn.execute(
        "UPDATE positions SET highest_close = ?, current_stop = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (highest_close, max(new_stop, row["current_stop"]), row["id"]))
    conn.commit()
    conn.close()


def get_open_positions() -> dict[str, dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM positions WHERE status = 'open'").fetchall()
    conn.close()
    return {r["symbol"]: dict(r) for r in rows}


# ---------------------------------------------------------------------------
# Fund state (initial capital)
# ---------------------------------------------------------------------------

def capture_initial_capital(available_cash: float) -> float | None:
    """Auto-captures "initial capital" from Kite's own available cash the
    first time it's non-zero. Written once (singleton row); never
    overwritten by later deposits or drawdowns."""
    conn = get_conn()
    row = conn.execute("SELECT initial_capital FROM fund_state WHERE id = 1").fetchone()
    if row is not None:
        conn.close()
        return row["initial_capital"]
    if available_cash > 0:
        conn.execute(
            "INSERT INTO fund_state (id, initial_capital, captured_date) "
            "VALUES (1, ?, ?)", (available_cash, dt.date.today().isoformat()))
        conn.commit()
        conn.close()
        return available_cash
    conn.close()
    return None


# ---------------------------------------------------------------------------
# Equity log
# ---------------------------------------------------------------------------

def log_equity_snapshot(value: float) -> pd.DataFrame:
    """Upserts today's portfolio value; returns the full log as a DataFrame
    with the same ["date", "value"] shape the Cockpit chart already expects."""
    conn = get_conn()
    today = dt.date.today().isoformat()
    conn.execute("INSERT OR REPLACE INTO equity_log (date, value) VALUES (?, ?)",
                (today, value))
    conn.commit()
    log = pd.read_sql("SELECT date, value FROM equity_log ORDER BY date", conn)
    conn.close()
    return log


# ---------------------------------------------------------------------------
# Rebalance run history (replaces rebalance_proposal.pkl)
# ---------------------------------------------------------------------------

def save_rebalance_run(result: dict) -> int:
    """Persists a propose_rebalance() result dict (run_time, sells, buys,
    stop_updates, holdings, open_slots) -- returns the new run_id."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO rebalance_runs (run_time, open_slots, status) "
        "VALUES (?, ?, 'success')",
        (result["run_time"].isoformat(), int(result["open_slots"])))
    run_id = cur.lastrowid
    for _, r in result["sells"].iterrows():
        conn.execute(
            "INSERT INTO rebalance_sells (run_id, symbol, qty, avg_price, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, r["symbol"], int(r["qty"]), float(r["avg_price"]), r["reason"]))
    for _, r in result["buys"].iterrows():
        fscore = r.get("fundamental_score")
        conn.execute(
            "INSERT INTO rebalance_buys (run_id, symbol, qty, price, stop, score, "
            "fundamental_score, fundamental_rubric) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, r["symbol"], int(r["qty"]), float(r["price"]), float(r["stop"]),
             float(r["score"]), None if pd.isna(fscore) else float(fscore),
             r.get("fundamental_rubric")))
    for _, r in result.get("stop_updates", pd.DataFrame()).iterrows():
        conn.execute(
            "INSERT INTO rebalance_stop_updates (run_id, symbol, qty, current_stop, "
            "recommended_stop, gtt_trigger_id) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, r["symbol"], int(r["qty"]), float(r["current_stop"]),
             float(r["recommended_stop"]),
             None if pd.isna(r["gtt_trigger_id"]) else int(r["gtt_trigger_id"])))
    conn.commit()
    conn.close()
    return run_id


def save_rebalance_failure(error_message: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO rebalance_runs (run_time, status, error_message) "
        "VALUES (?, 'failed', ?)", (dt.datetime.now().isoformat(), error_message))
    conn.commit()
    conn.close()


def get_last_rebalance_run() -> dict | None:
    """Same shape dashboard.py/live_rebalance.py already expect from the
    old pickle: {"run_time", "sells", "buys", "stop_updates", "open_slots"}
    (holdings isn't persisted -- callers already fetch that fresh from
    Kite, it's only ever a live snapshot, never historical)."""
    conn = get_conn()
    run = conn.execute(
        "SELECT * FROM rebalance_runs WHERE status = 'success' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if run is None:
        conn.close()
        return None
    run_id = run["id"]
    sells = pd.read_sql(
        "SELECT symbol, qty, avg_price, reason FROM rebalance_sells WHERE run_id = ?",
        conn, params=(run_id,))
    buys = pd.read_sql(
        "SELECT symbol, qty, price, stop, score, fundamental_score, "
        "fundamental_rubric FROM rebalance_buys WHERE run_id = ?",
        conn, params=(run_id,))
    stop_updates = pd.read_sql(
        "SELECT symbol, qty, current_stop, recommended_stop, gtt_trigger_id "
        "FROM rebalance_stop_updates WHERE run_id = ?", conn, params=(run_id,))
    conn.close()
    return {
        "run_time": dt.datetime.fromisoformat(run["run_time"]),
        "sells": sells, "buys": buys, "stop_updates": stop_updates,
        "open_slots": run["open_slots"],
    }
