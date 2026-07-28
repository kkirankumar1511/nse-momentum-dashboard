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
import hashlib
import json
import os
import secrets
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

-- Every deposit/withdrawal, dated -- Kite's API has no visibility into
-- bank transfers, so this is manually logged (Admin page). The old
-- fund_state singleton row above is superseded by this (a proper ledger
-- lets XIRR account for deposit timing, not just a single starting
-- amount) -- see _migrate_fund_state_to_cash_flow, which carries that row
-- forward as this ledger's first entry rather than losing it.
CREATE TABLE IF NOT EXISTS cash_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT
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

-- Dashboard login gate. Singleton row, password stored as a salted hash
-- (PBKDF2-HMAC-SHA256) -- never in plaintext, unlike the .env value this
-- replaces.
CREATE TABLE IF NOT EXISTS dashboard_auth (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL
);

-- Strategy parameters (config.STRATEGY), editable from the dashboard's
-- Admin page instead of only via a config.py code edit + restart. One row
-- per key, value JSON-encoded so int/float/bool/str all round-trip cleanly
-- through a single TEXT column. Seeded once from config.py's in-code
-- defaults (see get_strategy_config) -- after that, the DB is the live
-- source of truth, same pattern as kite_credentials/dashboard_auth above.
CREATE TABLE IF NOT EXISTS strategy_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Kite Connect credentials. Singleton row. api_key/api_secret are stored
-- plaintext -- unlike dashboard_auth's password, these must be recoverable
-- (Kite's OAuth exchange needs the real api_secret value), so this is a
-- consolidation move, not a security upgrade, for those two. access_token
-- is a better fit for a DB than .env ever was: it's genuinely frequent-
-- changing live state (expires ~daily), same category as everything else
-- in this file.
CREATE TABLE IF NOT EXISTS kite_credentials (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    api_key TEXT NOT NULL,
    api_secret TEXT NOT NULL,
    access_token TEXT,
    access_token_updated_at TEXT
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
    _migrate_fund_state_to_cash_flow(conn)
    _migrate_positions_schema(conn)
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


def _migrate_fund_state_to_cash_flow(conn: sqlite3.Connection) -> None:
    """One-time carry-over of the old fund_state singleton row (initial
    capital, auto-captured once) as the first cash_flows ledger entry --
    preserves the original captured date/amount instead of losing it when
    the ledger takes over. No-ops once cash_flows already has rows, or if
    fund_state was never populated."""
    existing = conn.execute("SELECT COUNT(*) FROM cash_flows").fetchone()[0]
    if existing:
        return
    row = conn.execute("SELECT * FROM fund_state WHERE id = 1").fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT INTO cash_flows (date, amount, note) VALUES (?, ?, ?)",
        (row["captured_date"], row["initial_capital"],
         "Migrated from initial capital auto-capture"))
    conn.commit()


def _migrate_positions_schema(conn: sqlite3.Connection) -> None:
    """Adds exit_price/realized_pnl columns to an already-existing positions
    table -- CREATE TABLE IF NOT EXISTS above doesn't touch a table that
    already exists, so new columns need this explicit, idempotent check."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(positions)")}
    if "exit_price" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN exit_price REAL")
    if "realized_pnl" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN realized_pnl REAL")
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


def get_stale_open_symbols(held_symbols: set[str]) -> list[str]:
    """Open positions no longer in held_symbols -- about to be closed on the
    next reconciled_positions() call. Callers fetch an LTP for these first
    (e.g. via kite_client.get_ltp()) so reconciled_positions() can record a
    realized exit price -- state_db.py itself can't call kite_client (it
    would be a circular import, since kite_client already imports state_db)."""
    conn = get_conn()
    open_rows = conn.execute(
        "SELECT symbol FROM positions WHERE status = 'open'").fetchall()
    conn.close()
    return [r["symbol"] for r in open_rows if r["symbol"] not in held_symbols]


def reconciled_positions(held_symbols: set[str],
                         exit_prices: dict[str, float] | None = None) -> dict[str, dict]:
    """Marks any 'open' row whose symbol isn't in held_symbols as 'closed'
    (closed_date=today) instead of deleting -- a GTT can close a position
    without any of this app's code running, so every read reconciles
    against real Kite holdings first; history is preserved rather than
    silently dropped. Returns the remaining open positions, keyed by
    symbol, same dict shape the old JSON version returned.

    exit_prices, if supplied (keyed by symbol, from get_stale_open_symbols()
    + a fresh LTP fetch), also records exit_price and
    realized_pnl = (exit_price - entry_price) * qty on the closing row --
    Kite's API only exposes today's trades/orders, no historical realized-
    P&L endpoint, so this LTP-at-detection-time approximation is this app's
    own reconstruction of it. Left null if no price was supplied for a
    given symbol (caller chose to skip the extra LTP call)."""
    exit_prices = exit_prices or {}
    conn = get_conn()
    open_rows = conn.execute(
        "SELECT * FROM positions WHERE status = 'open'").fetchall()
    today = dt.date.today().isoformat()
    for row in open_rows:
        if row["symbol"] not in held_symbols:
            exit_price = exit_prices.get(row["symbol"])
            realized_pnl = ((exit_price - row["entry_price"]) * row["qty"]
                           if exit_price is not None else None)
            conn.execute(
                "UPDATE positions SET status = 'closed', closed_date = ?, "
                "exit_price = ?, realized_pnl = ? WHERE id = ?",
                (today, exit_price, realized_pnl, row["id"]))
    conn.commit()
    remaining = conn.execute(
        "SELECT * FROM positions WHERE status = 'open'").fetchall()
    conn.close()
    return {r["symbol"]: dict(r) for r in remaining}


def get_realized_pnl() -> float:
    """SUM(realized_pnl) over all closed positions -- null entries (closed
    without a supplied exit price) don't contribute, same as SQL SUM's
    normal NULL handling."""
    conn = get_conn()
    row = conn.execute(
        "SELECT SUM(realized_pnl) AS total FROM positions "
        "WHERE status = 'closed'").fetchone()
    conn.close()
    return float(row["total"]) if row["total"] is not None else 0.0


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


def upsert_manual_position(symbol: str, entry_price: float, qty: int,
                           stop: float, gtt_trigger_id: int | None) -> None:
    """Used when placing a stop-loss for a position this app didn't itself
    open (e.g. bought directly on Kite, outside the Live Rebalance/manual-
    order flows) -- creates a new open position row if none exists yet
    (using Kite's own real entry_price/qty for that symbol), or just updates
    the current_stop/gtt_trigger_id if one already does (e.g. re-placing a
    GTT that expired or was cancelled). Either way, the position becomes
    fully known to reconciled_positions()/get_open_positions() afterward, so
    future trailing-stop updates pick it up like any other position."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM positions WHERE symbol = ? AND status = 'open'",
        (symbol,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO positions (symbol, entry_date, entry_price, qty, "
            "highest_close, current_stop, gtt_trigger_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
            (symbol, dt.date.today().isoformat(), entry_price, qty,
             entry_price, stop, gtt_trigger_id))
    else:
        conn.execute(
            "UPDATE positions SET current_stop = ?, gtt_trigger_id = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (stop, gtt_trigger_id, row["id"]))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Cash-flow ledger -- every deposit/withdrawal, dated. Supersedes the old
# fund_state singleton row (see _migrate_fund_state_to_cash_flow): a proper
# ledger lets XIRR account for deposit timing, not just a single starting
# amount, matching the user's stated plan to add money monthly.
# ---------------------------------------------------------------------------

def record_cash_flow(date: str, amount: float, note: str = "") -> None:
    """Manual entry -- called from the Admin page's deposit/withdrawal
    form. amount is positive for a deposit, negative for a withdrawal.
    Kite's API has no visibility into bank transfers, so this can't be
    automated beyond the very first entry (see
    ensure_first_cash_flow_captured)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO cash_flows (date, amount, note) VALUES (?, ?, ?)",
        (date, amount, note))
    conn.commit()
    conn.close()


def get_cash_flows() -> pd.DataFrame:
    """Full ledger, oldest first -- feeds the XIRR calc and the Admin
    page's audit-trail table."""
    conn = get_conn()
    log = pd.read_sql(
        "SELECT date, amount, note FROM cash_flows ORDER BY date, id", conn)
    conn.close()
    return log


def ensure_first_cash_flow_captured(available_cash: float) -> None:
    """Auto-captures the very first deposit from Kite's own available cash,
    the first time it's non-zero and the ledger is still empty -- same
    first-time-only behavior capture_initial_capital used to provide, so
    the user isn't required to manually log money already sitting in the
    account. Every deposit after this one is manual (Admin page), per the
    user's plan to add money monthly."""
    conn = get_conn()
    existing = conn.execute("SELECT COUNT(*) FROM cash_flows").fetchone()[0]
    conn.close()
    if existing or available_cash <= 0:
        return
    record_cash_flow(dt.date.today().isoformat(), available_cash,
                     "Auto-captured initial available cash")


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


# ---------------------------------------------------------------------------
# Dashboard login gate -- replaces plaintext DASHBOARD_USERNAME/PASSWORD in
# .env with a salted hash stored here. The password itself is never stored,
# only PBKDF2-HMAC-SHA256(password, salt, 200_000 rounds) -- a standard,
# NIST-recommended construction available in the stdlib (hashlib), no new
# dependency needed.
# ---------------------------------------------------------------------------

def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()


def ensure_dashboard_auth_seeded(default_username: str, default_password: str) -> None:
    """First-run only: seeds the singleton row from the given defaults
    (typically config.DASHBOARD_USERNAME/PASSWORD, themselves defaulting to
    the Admin/Admin placeholder) -- hashed immediately, never held in
    plaintext past this call. No-ops once a row already exists, so this is
    safe to call on every dashboard load."""
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM dashboard_auth WHERE id = 1").fetchone()
    if row is None:
        salt = secrets.token_bytes(16)
        conn.execute(
            "INSERT INTO dashboard_auth (id, username, password_hash, salt) "
            "VALUES (1, ?, ?, ?)",
            (default_username, _hash_password(default_password, salt), salt.hex()))
        conn.commit()
    conn.close()


def verify_dashboard_login(username: str, password: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT * FROM dashboard_auth WHERE id = 1").fetchone()
    conn.close()
    if row is None:
        return False
    salt = bytes.fromhex(row["salt"])
    return username == row["username"] and _hash_password(password, salt) == row["password_hash"]


def update_dashboard_password(username: str, new_password: str) -> None:
    """Overwrites the singleton row -- used by the dashboard's own
    change-password form, so a password can be changed without touching
    .env or restarting the process."""
    conn = get_conn()
    salt = secrets.token_bytes(16)
    conn.execute(
        "UPDATE dashboard_auth SET username = ?, password_hash = ?, salt = ? "
        "WHERE id = 1",
        (username, _hash_password(new_password, salt), salt.hex()))
    conn.commit()
    conn.close()


def is_using_default_dashboard_password(default_username: str, default_password: str) -> bool:
    """For the loud on-screen warning -- true only while still on the
    seeded Admin/Admin-style default, false the moment it's ever changed."""
    return verify_dashboard_login(default_username, default_password)


# ---------------------------------------------------------------------------
# Strategy configuration -- config.STRATEGY, editable from the Admin page
# instead of only via a code edit + restart.
# ---------------------------------------------------------------------------

def get_strategy_config(defaults: dict) -> dict:
    """Returns the live strategy config, DB values taking precedence over
    `defaults` key by key. Self-healing like every other table here: any
    key in `defaults` missing from the DB (first run, or a new parameter
    added to config.py after the DB already existed) gets seeded from
    `defaults` and returned as-is -- so adding a new STRATEGY key later
    doesn't require a manual migration, only a code change to config.py."""
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM strategy_config").fetchall()
    stored = {r["key"]: json.loads(r["value"]) for r in rows}
    missing = {k: v for k, v in defaults.items() if k not in stored}
    if missing:
        conn.executemany(
            "INSERT INTO strategy_config (key, value) VALUES (?, ?)",
            [(k, json.dumps(v)) for k, v in missing.items()])
        conn.commit()
    conn.close()
    return {**defaults, **stored, **missing}


def update_strategy_config(updates: dict) -> None:
    """Upserts the given {key: value} pairs -- used by the Admin page's
    strategy settings form. Only ever called with keys that already exist
    (from a form pre-filled by get_strategy_config), but INSERT OR REPLACE
    handles a brand-new key just as well."""
    conn = get_conn()
    conn.executemany(
        "INSERT OR REPLACE INTO strategy_config (key, value) VALUES (?, ?)",
        [(k, json.dumps(v)) for k, v in updates.items()])
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Kite Connect credentials -- replaces KITE_API_KEY/KITE_API_SECRET/
# KITE_ACCESS_TOKEN in .env. api_key/api_secret stay plaintext (must be
# recoverable, unlike a password); access_token is genuinely a better fit
# here than .env ever was, since it's frequently-changing live state
# (expires roughly daily), the same category as everything else in this
# file.
# ---------------------------------------------------------------------------

def ensure_kite_credentials_seeded(api_key: str, api_secret: str,
                                   access_token: str = "") -> None:
    """First-run only: seeds from whatever's currently in .env (config.py's
    fallback values). No-ops once a row exists, so this never clobbers a
    token/key that's since been updated through the DB directly."""
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM kite_credentials WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO kite_credentials (id, api_key, api_secret, "
            "access_token, access_token_updated_at) VALUES (1, ?, ?, ?, ?)",
            (api_key, api_secret, access_token,
             dt.datetime.now().isoformat() if access_token else None))
        conn.commit()
    conn.close()


def get_kite_credentials() -> dict:
    """Returns {"api_key", "api_secret", "access_token", ...} or all-empty
    if nothing has been seeded yet (fresh install, no .env values either)."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM kite_credentials WHERE id = 1").fetchone()
    conn.close()
    if row is None:
        return {"api_key": "", "api_secret": "", "access_token": "",
               "access_token_updated_at": None}
    return dict(row)


def save_kite_access_token(token: str) -> None:
    """Called after a successful OAuth exchange -- see
    kite_client.exchange_request_token(). Only updates the token, leaves
    api_key/api_secret untouched."""
    conn = get_conn()
    conn.execute(
        "UPDATE kite_credentials SET access_token = ?, "
        "access_token_updated_at = ? WHERE id = 1",
        (token, dt.datetime.now().isoformat()))
    conn.commit()
    conn.close()


def update_kite_api_credentials(api_key: str, api_secret: str) -> None:
    """Used by the dashboard's Kite API settings form, for whenever the
    user regenerates keys in the Kite developer console."""
    conn = get_conn()
    conn.execute(
        "UPDATE kite_credentials SET api_key = ?, api_secret = ? WHERE id = 1",
        (api_key, api_secret))
    conn.commit()
    conn.close()
