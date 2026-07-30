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

import contextlib
import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
import time
import traceback

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
    error_message TEXT,
    target_per_slot REAL,
    cash_pool REAL,
    cash_needed_for_full_equal_weight REAL,
    cash_shortfall REAL,
    unsettled_proceeds REAL
);

CREATE TABLE IF NOT EXISTS rebalance_sells (
    run_id INTEGER REFERENCES rebalance_runs(id),
    symbol TEXT, qty INTEGER, avg_price REAL, reason TEXT
);

CREATE TABLE IF NOT EXISTS rebalance_buys (
    run_id INTEGER REFERENCES rebalance_runs(id),
    symbol TEXT, qty INTEGER, price REAL, stop REAL, score REAL,
    fundamental_score REAL, fundamental_rubric TEXT,
    rsi REAL, pct_52w_high REAL, vol_expansion REAL, reason TEXT
);

CREATE TABLE IF NOT EXISTS rebalance_stop_updates (
    run_id INTEGER REFERENCES rebalance_runs(id),
    symbol TEXT, qty INTEGER, current_stop REAL, recommended_stop REAL,
    gtt_trigger_id INTEGER
);

-- Proposed top-ups (screener.allocate_equal_weight_buys) -- additional
-- shares for an ALREADY-held position that's below its equal-weight
-- target, funded by cash left over after filling new-buy slots. Distinct
-- from rebalance_buys (which only ever opens brand-new positions).
CREATE TABLE IF NOT EXISTS rebalance_top_ups (
    run_id INTEGER REFERENCES rebalance_runs(id),
    symbol TEXT, extra_qty INTEGER, price REAL, gtt_trigger_id INTEGER
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

-- Unified execution log for every scheduled/background job (rebalance scan,
-- gap-down check, fundamentals refresh, and the dashboard's own manual
-- "Run screen"/"Run today's scan" buttons) -- see job_run() below. Before
-- this, only the rebalance scan had any persisted history at all
-- (rebalance_runs), and the other jobs had nothing queryable, only a
-- plain-text tail log.
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_sec REAL,
    status TEXT NOT NULL DEFAULT 'running',
    summary TEXT,
    error_message TEXT
);

-- The tradebook -- a clean, append-only analytics ledger, separate from
-- `positions` (which stays focused on live trailing-stop bookkeeping).
-- Captures an entry-time technical/fundamental snapshot (for later
-- feature analytics) and a real exit reason on every closed trade, which
-- nothing in `positions` tracked before this.
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES positions(id),
    symbol TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    qty INTEGER NOT NULL,
    initial_stop REAL NOT NULL,
    entry_score REAL,
    entry_rsi REAL,
    entry_pct_52w_high REAL,
    entry_vol_expansion REAL,
    entry_fundamental_score REAL,
    entry_reason TEXT,
    exit_date TEXT,
    exit_price REAL,
    exit_reason TEXT,
    realized_pnl REAL,
    realized_ret_pct REAL,
    holding_days INTEGER,
    status TEXT NOT NULL DEFAULT 'open'
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
    _migrate_stop_update_log_schema(conn)
    _migrate_trades_schema(conn)
    _migrate_rebalance_buys_schema(conn)
    _migrate_rebalance_runs_schema(conn)
    _migrate_equity_log_schema(conn)
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
    """Adds exit_price/realized_pnl/recommended_stop columns to an
    already-existing positions table -- CREATE TABLE IF NOT EXISTS above
    doesn't touch a table that already exists, so new columns need this
    explicit, idempotent check.

    recommended_stop: the latest trailing-stop VALUE COMPUTED, shown to the
    user for approval -- current_stop now means only the APPLIED stop (what
    the real broker GTT is actually set to), never written outside
    apply_stop_update(). Before this column existed, update_position_stop()
    wrote straight into current_stop every day regardless of whether the
    user had actually applied it, so main_gap_check()'s gap-down comparison
    (live_rebalance.py) could silently compare against a stop the broker
    was never told about -- see apply_stop_update()'s docstring."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(positions)")}
    if "exit_price" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN exit_price REAL")
    if "realized_pnl" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN realized_pnl REAL")
    if "recommended_stop" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN recommended_stop REAL")
    conn.commit()


def _migrate_trades_schema(conn: sqlite3.Connection) -> None:
    """Adds entry_reason to an already-existing trades table -- a
    human-readable one-liner (rank, score, RSI, fundamental score) built at
    entry time, alongside the raw numeric snapshot columns, so the
    tradebook is readable at a glance without cross-referencing every
    number by hand."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)")}
    if "entry_reason" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN entry_reason TEXT")
    conn.commit()


def _migrate_rebalance_buys_schema(conn: sqlite3.Connection) -> None:
    """Adds rsi/pct_52w_high/vol_expansion/reason to an already-existing
    rebalance_buys table -- these were already in propose_rebalance()'s
    in-memory buys dict but never persisted, so a page reload (reading
    back via get_last_rebalance_run() instead of a fresh propose_rebalance()
    call) would silently lose them."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(rebalance_buys)")}
    for col, coltype in [("rsi", "REAL"), ("pct_52w_high", "REAL"),
                        ("vol_expansion", "REAL"), ("reason", "TEXT")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE rebalance_buys ADD COLUMN {col} {coltype}")
    conn.commit()


def _migrate_rebalance_runs_schema(conn: sqlite3.Connection) -> None:
    """Adds target_per_slot/cash_pool/cash_needed_for_full_equal_weight/
    cash_shortfall to an already-existing rebalance_runs table -- without
    this, those figures only ever lived in the in-memory result dict from
    the run that just computed them, and would silently vanish the moment
    a page reload re-read the proposal via get_last_rebalance_run()
    instead."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(rebalance_runs)")}
    for col in ["target_per_slot", "cash_pool",
               "cash_needed_for_full_equal_weight", "cash_shortfall",
               "unsettled_proceeds"]:
        if col not in cols:
            conn.execute(f"ALTER TABLE rebalance_runs ADD COLUMN {col} REAL")
    conn.commit()


def _migrate_equity_log_schema(conn: sqlite3.Connection) -> None:
    """Adds invested_amount to an already-existing equity_log table -- lets
    the Overview page's chart overlay cost basis alongside total portfolio
    value, so growth from new capital vs actual returns is visually
    distinguishable. Only ever populated going forward (log_equity_snapshot
    is called once/day) -- there's no way to backfill historical cost
    basis for days before this column existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(equity_log)")}
    if "invested_amount" not in cols:
        conn.execute("ALTER TABLE equity_log ADD COLUMN invested_amount REAL")
    conn.commit()


def _migrate_stop_update_log_schema(conn: sqlite3.Connection) -> None:
    """Adds atr_value/ratcheted columns to an already-existing
    stop_update_log table. atr_value: the raw ATR reading that day's stop
    was computed from (never persisted before this -- only the resulting
    old_stop/new_stop pair was, and only on days the stop actually
    ratcheted). ratcheted: whether THIS row's check actually raised the
    stop -- update_position_stop() now inserts a row every day it runs, not
    only on ratchet days, so a continuous daily history exists."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(stop_update_log)")}
    if "atr_value" not in cols:
        conn.execute("ALTER TABLE stop_update_log ADD COLUMN atr_value REAL")
    if "ratcheted" not in cols:
        conn.execute("ALTER TABLE stop_update_log ADD COLUMN ratcheted INTEGER NOT NULL DEFAULT 0")
    conn.commit()


# ---------------------------------------------------------------------------
# Positions (trailing-stop tracking)
# ---------------------------------------------------------------------------

def record_new_position(symbol: str, entry_price: float, qty: int,
                        stop: float, gtt_trigger_id: int | None) -> int:
    """Call right after a buy (+ GTT, if placed) succeeds -- seeds this
    symbol's trailing-stop bookkeeping. gtt_trigger_id=None if the GTT
    placement failed or was skipped. Returns the new position's row id --
    pass it to record_trade_entry() as position_id to link the tradebook
    row back to this position."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO positions (symbol, entry_date, entry_price, qty, "
        "highest_close, current_stop, gtt_trigger_id, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
        (symbol, dt.date.today().isoformat(), entry_price, qty,
         entry_price, stop, gtt_trigger_id))
    position_id = cur.lastrowid
    conn.commit()
    conn.close()
    return position_id


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
    given symbol (caller chose to skip the extra LTP call).

    Also closes the matching trades row (see close_trade()) tagged
    exit_reason='gtt_fill_or_external' -- this is the guaranteed-leftover
    path: every KNOWN exit (gap-down stop, rebalance sell, manual
    square-off) closes its trades row explicitly, with its own specific
    reason, before this function next runs for that symbol. Anything still
    caught here can only be a GTT that fired silently at the broker, or an
    out-of-band manual sell outside this app -- close_trade() no-ops
    harmlessly if an explicit call already closed the trade first."""
    exit_prices = exit_prices or {}
    conn = get_conn()
    open_rows = conn.execute(
        "SELECT * FROM positions WHERE status = 'open'").fetchall()
    today = dt.date.today().isoformat()
    newly_closed = []
    for row in open_rows:
        if row["symbol"] not in held_symbols:
            exit_price = exit_prices.get(row["symbol"])
            realized_pnl = ((exit_price - row["entry_price"]) * row["qty"]
                           if exit_price is not None else None)
            conn.execute(
                "UPDATE positions SET status = 'closed', closed_date = ?, "
                "exit_price = ?, realized_pnl = ? WHERE id = ?",
                (today, exit_price, realized_pnl, row["id"]))
            newly_closed.append((row["symbol"], exit_price))
    conn.commit()
    remaining = conn.execute(
        "SELECT * FROM positions WHERE status = 'open'").fetchall()
    conn.close()
    for symbol, exit_price in newly_closed:
        close_trade(symbol, exit_price, "gtt_fill_or_external")
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


def update_position_stop(symbol: str, highest_close: float, atr_value: float,
                         new_stop: float) -> None:
    """Called once daily (from live_rebalance.py's compute_stop_updates())
    for every open position, ratchet or not -- records a stop_update_log
    row EVERY time (atr_value + whether this check actually ratcheted the
    stop), so a continuous daily ATR/stop history exists, not just a trail
    of ratchet events.

    Writes the computed candidate into positions.recommended_stop only --
    never positions.current_stop. current_stop means the stop actually
    APPLIED at the broker (see apply_stop_update()) and must only change
    when an Apply action really pushes it to the real GTT; main_gap_check()
    compares live LTP against current_stop specifically because it needs
    the real, broker-side stop, not a theoretical unapplied one."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM positions WHERE symbol = ? AND status = 'open'",
        (symbol,)).fetchone()
    if row is None:
        conn.close()
        return
    ratcheted = new_stop > row["current_stop"]
    conn.execute(
        "INSERT INTO stop_update_log (position_id, date, old_stop, "
        "new_stop, atr_value, ratcheted, applied) VALUES (?, ?, ?, ?, ?, ?, 0)",
        (row["id"], dt.date.today().isoformat(), row["current_stop"],
         new_stop, atr_value, int(ratcheted)))
    conn.execute(
        "UPDATE positions SET highest_close = ?, recommended_stop = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (highest_close, max(new_stop, row["current_stop"]), row["id"]))
    conn.commit()
    conn.close()


def apply_stop_update(symbol: str) -> float | None:
    """Call right after kite_client.modify_gtt_trigger(...) succeeds for
    this symbol's open position -- copies recommended_stop into
    current_stop (the applied, broker-real stop) and marks that position's
    most recent stop_update_log row as applied. Returns the new applied
    stop, or None if there's no open position / nothing recommended.

    Only the MOST RECENT stop_update_log row is marked applied, not every
    unapplied one -- if a stop was recommended on day 1 but not applied
    until day 3 (by which point day 2 had already recommended a higher
    value), only day 2's row ever actually reached the broker; day 1's
    row stays unapplied, accurately reflecting that its value was
    superseded before ever being pushed."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM positions WHERE symbol = ? AND status = 'open'",
        (symbol,)).fetchone()
    if row is None or row["recommended_stop"] is None:
        conn.close()
        return None
    new_current = row["recommended_stop"]
    conn.execute(
        "UPDATE positions SET current_stop = ?, updated_at = datetime('now') "
        "WHERE id = ?", (new_current, row["id"]))
    last_log_id = conn.execute(
        "SELECT id FROM stop_update_log WHERE position_id = ? "
        "ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    if last_log_id is not None:
        conn.execute("UPDATE stop_update_log SET applied = 1 WHERE id = ?",
                    (last_log_id["id"],))
    conn.commit()
    conn.close()
    return float(new_current)


def get_open_positions() -> dict[str, dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM positions WHERE status = 'open'").fetchall()
    conn.close()
    return {r["symbol"]: dict(r) for r in rows}


def top_up_trade(symbol: str, extra_qty: int, price: float) -> None:
    """Call right after buying MORE shares of an ALREADY-open position
    (screener.allocate_equal_weight_buys' top-up mechanic) succeeds --
    weighted-averages the cost basis into both the positions row (qty,
    entry_price) and the matching open trades row, mirroring backtest.py's
    top_up_position(). Distinct from record_new_position(), which only
    ever opens a brand-new position. No-ops if there's no open position
    for this symbol."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM positions WHERE symbol = ? AND status = 'open'",
        (symbol,)).fetchone()
    if row is None or extra_qty <= 0:
        conn.close()
        return
    new_qty = row["qty"] + extra_qty
    new_entry_price = (row["entry_price"] * row["qty"] + price * extra_qty) / new_qty
    conn.execute(
        "UPDATE positions SET qty = ?, entry_price = ?, updated_at = datetime('now') "
        "WHERE id = ?", (new_qty, new_entry_price, row["id"]))
    conn.commit()
    conn.close()

    trades_conn = get_conn()
    trade_row = trades_conn.execute(
        "SELECT * FROM trades WHERE symbol = ? AND status = 'open' "
        "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
    if trade_row is not None:
        t_new_qty = trade_row["qty"] + extra_qty
        t_new_entry = (trade_row["entry_price"] * trade_row["qty"]
                      + price * extra_qty) / t_new_qty
        trades_conn.execute(
            "UPDATE trades SET qty = ?, entry_price = ? WHERE id = ?",
            (t_new_qty, t_new_entry, trade_row["id"]))
        trades_conn.commit()
    trades_conn.close()


def upsert_manual_position(symbol: str, entry_price: float, qty: int,
                           stop: float, gtt_trigger_id: int | None) -> None:
    """Used when placing a stop-loss for a position this app didn't itself
    open (e.g. bought directly on Kite, outside the Live Rebalance/manual-
    order flows) -- creates a new open position row if none exists yet
    (using Kite's own real entry_price/qty for that symbol), or just updates
    the current_stop/gtt_trigger_id if one already does (e.g. re-placing a
    GTT that expired or was cancelled). Either way, the position becomes
    fully known to reconciled_positions()/get_open_positions() afterward, so
    future trailing-stop updates pick it up like any other position.

    Also seeds a matching trades row (empty snapshot -- no screener data
    exists for a position this app didn't pick) on the first-insert path
    only, so this position's eventual close still has an open trade row
    for close_trade() to find -- otherwise it would only ever be caught by
    reconciled_positions()'s generic fallback with no trades row to close
    at all, silently missing it from the tradebook entirely."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM positions WHERE symbol = ? AND status = 'open'",
        (symbol,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO positions (symbol, entry_date, entry_price, qty, "
            "highest_close, current_stop, gtt_trigger_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
            (symbol, dt.date.today().isoformat(), entry_price, qty,
             entry_price, stop, gtt_trigger_id))
        new_position_id = cur.lastrowid
        conn.commit()
        conn.close()
        record_trade_entry(
            symbol, entry_price, qty, stop,
            snapshot={"entry_reason": "Backfilled -- bought outside this app "
                     "(e.g. directly on Kite), stop-loss added here after the fact"},
            position_id=new_position_id)
        return
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

def log_equity_snapshot(value: float, invested_amount: float | None = None) -> pd.DataFrame:
    """Upserts today's portfolio value (and optionally cost basis, for the
    Overview page's chart overlay); returns the full log as a DataFrame
    with ["date", "value", "invested_amount"]. Uses COALESCE on conflict
    rather than a blind overwrite -- this runs on every page load, so a
    later call that doesn't pass invested_amount (or an older caller that
    doesn't know about it) won't silently wipe out a value an earlier
    call already recorded for today."""
    conn = get_conn()
    today = dt.date.today().isoformat()
    conn.execute(
        "INSERT INTO equity_log (date, value, invested_amount) VALUES (?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET value = excluded.value, "
        "invested_amount = COALESCE(excluded.invested_amount, equity_log.invested_amount)",
        (today, value, invested_amount))
    conn.commit()
    log = pd.read_sql(
        "SELECT date, value, invested_amount FROM equity_log ORDER BY date", conn)
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
        "INSERT INTO rebalance_runs (run_time, open_slots, status, "
        "target_per_slot, cash_pool, cash_needed_for_full_equal_weight, "
        "cash_shortfall, unsettled_proceeds) VALUES (?, ?, 'success', ?, ?, ?, ?, ?)",
        (result["run_time"].isoformat(), int(result["open_slots"]),
         result.get("target_per_slot"), result.get("cash_pool"),
         result.get("cash_needed_for_full_equal_weight"),
         result.get("cash_shortfall"), result.get("unsettled_proceeds")))
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
            "fundamental_score, fundamental_rubric, rsi, pct_52w_high, "
            "vol_expansion, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, r["symbol"], int(r["qty"]), float(r["price"]), float(r["stop"]),
             float(r["score"]), None if pd.isna(fscore) else float(fscore),
             r.get("fundamental_rubric"), r.get("rsi"), r.get("pct_52w_high"),
             r.get("vol_expansion"), r.get("reason")))
    for _, r in result.get("stop_updates", pd.DataFrame()).iterrows():
        conn.execute(
            "INSERT INTO rebalance_stop_updates (run_id, symbol, qty, current_stop, "
            "recommended_stop, gtt_trigger_id) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, r["symbol"], int(r["qty"]), float(r["current_stop"]),
             float(r["recommended_stop"]),
             None if pd.isna(r["gtt_trigger_id"]) else int(r["gtt_trigger_id"])))
    for _, r in result.get("top_ups", pd.DataFrame()).iterrows():
        conn.execute(
            "INSERT INTO rebalance_top_ups (run_id, symbol, extra_qty, price, "
            "gtt_trigger_id) VALUES (?, ?, ?, ?, ?)",
            (run_id, r["symbol"], int(r["extra_qty"]), float(r["price"]),
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
        "fundamental_rubric, rsi, pct_52w_high, vol_expansion, reason "
        "FROM rebalance_buys WHERE run_id = ?",
        conn, params=(run_id,))
    stop_updates = pd.read_sql(
        "SELECT symbol, qty, current_stop, recommended_stop, gtt_trigger_id "
        "FROM rebalance_stop_updates WHERE run_id = ?", conn, params=(run_id,))
    top_ups = pd.read_sql(
        "SELECT symbol, extra_qty, price, gtt_trigger_id "
        "FROM rebalance_top_ups WHERE run_id = ?", conn, params=(run_id,))
    conn.close()
    return {
        "run_time": dt.datetime.fromisoformat(run["run_time"]),
        "sells": sells, "buys": buys, "stop_updates": stop_updates,
        "top_ups": top_ups, "open_slots": run["open_slots"],
        "target_per_slot": run["target_per_slot"], "cash_pool": run["cash_pool"],
        "cash_needed_for_full_equal_weight": run["cash_needed_for_full_equal_weight"],
        "cash_shortfall": run["cash_shortfall"],
        "unsettled_proceeds": run["unsettled_proceeds"],
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


# ---------------------------------------------------------------------------
# Job execution log -- unified history for every scheduled/background job
# (rebalance scan, gap-down check, fundamentals refresh, and the
# dashboard's manual "Run screen"/"Run today's scan" buttons). Wrap a job's
# body in the job_run() context manager below rather than calling
# start_job_run/finish_job_run directly.
# ---------------------------------------------------------------------------

def start_job_run(job_type: str, trigger_type: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO job_runs (job_type, trigger_type, started_at, status) "
        "VALUES (?, ?, ?, 'running')",
        (job_type, trigger_type, dt.datetime.now().isoformat()))
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    return run_id


def finish_job_run(run_id: int, status: str, summary: str | None = None,
                   error: str | None = None) -> None:
    conn = get_conn()
    row = conn.execute("SELECT started_at FROM job_runs WHERE id = ?",
                       (run_id,)).fetchone()
    started = dt.datetime.fromisoformat(row["started_at"])
    finished = dt.datetime.now()
    conn.execute(
        "UPDATE job_runs SET finished_at = ?, duration_sec = ?, status = ?, "
        "summary = ?, error_message = ? WHERE id = ?",
        (finished.isoformat(), (finished - started).total_seconds(), status,
         summary, error, run_id))
    conn.commit()
    conn.close()


@contextlib.contextmanager
def job_run(job_type: str, trigger_type: str):
    """Wrap a job's entire body in this. Records a 'running' row
    immediately, then 'success' or 'failed' (with the full traceback) when
    the block exits -- and always re-raises on failure, so a systemd unit
    still exits non-zero / a caller still sees the exception; this only
    ADDS a persisted record, it never swallows an error.

    Yields a plain dict -- set result["summary"] inside the `with` block to
    whatever one-line, job-type-specific text should show in the Job Log
    (e.g. "3 buys, 1 sell, 2 stop updates"); it's read only after the block
    finishes successfully.

    Usage:
        with state_db.job_run("rebalance_scan", "scheduled") as result:
            outcome = propose_rebalance(...)
            result["summary"] = f"{len(outcome['buys'])} buys, ..."
    """
    run_id = start_job_run(job_type, trigger_type)
    result: dict = {"summary": None}
    try:
        yield result
    except Exception:
        finish_job_run(run_id, "failed", error=traceback.format_exc())
        raise
    else:
        finish_job_run(run_id, "success", summary=result.get("summary"))


def get_job_runs(job_type: str | None = None, status: str | None = None,
                 since: str | None = None, limit: int = 200) -> pd.DataFrame:
    """since: an ISO date/datetime string, inclusive lower bound on
    started_at. All filters optional -- omit to get the unfiltered history
    (most recent `limit` rows)."""
    conn = get_conn()
    where, params = [], []
    if job_type:
        where.append("job_type = ?")
        params.append(job_type)
    if status:
        where.append("status = ?")
        params.append(status)
    if since:
        where.append("started_at >= ?")
        params.append(since)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    df = pd.read_sql(
        f"SELECT * FROM job_runs {clause} ORDER BY id DESC LIMIT ?",
        conn, params=params + [limit])
    conn.close()
    return df


def get_last_job_run(job_type: str) -> dict | None:
    """For the Job Log page's quick-glance strip -- last run of one job
    type, whatever its status."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM job_runs WHERE job_type = ? ORDER BY id DESC LIMIT 1",
        (job_type,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Tradebook -- append-only analytics ledger, separate from `positions`
# (which stays focused on live trailing-stop bookkeeping). One row per
# trade, capturing an entry-time technical/fundamental snapshot and a real
# exit reason -- neither existed anywhere before this.
# ---------------------------------------------------------------------------

def record_trade_entry(symbol: str, entry_price: float, qty: int, stop: float,
                       snapshot: dict, position_id: int | None = None,
                       entry_date: str | None = None) -> int:
    """snapshot: whatever entry-time context is available, keyed by the
    trades columns it maps to -- score/rsi/pct_52w_high/vol_expansion/
    fundamental_score/entry_reason. Missing keys are left null rather than
    required, since not every caller (e.g. a manual position add) has a
    full screener row to draw from.

    snapshot["entry_reason"]: a human-readable one-liner built by the
    caller from the same numbers (e.g. "Ranked #2 of 9 momentum candidates
    (score 2.39); RSI 58, 92% of 52w high; fundamental score 87/100") --
    see live_rebalance.py's propose_rebalance() buy loop for how it's
    constructed. Left null if the caller doesn't have enough context
    (manual/backfilled positions) rather than fabricated.

    entry_date: defaults to today (the normal case, called right after a
    live buy) -- pass an explicit ISO date to backfill a trade that
    happened before this table existed (e.g. from positions.entry_date +
    rebalance_buys' recorded score for that day)."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO trades (position_id, symbol, entry_date, entry_price, "
        "qty, initial_stop, entry_score, entry_rsi, entry_pct_52w_high, "
        "entry_vol_expansion, entry_fundamental_score, entry_reason, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
        (position_id, symbol, entry_date or dt.date.today().isoformat(),
         entry_price, qty, stop, snapshot.get("score"), snapshot.get("rsi"),
         snapshot.get("pct_52w_high"), snapshot.get("vol_expansion"),
         snapshot.get("fundamental_score"), snapshot.get("entry_reason")))
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def close_trade(symbol: str, exit_price: float | None, exit_reason: str) -> None:
    """Closes the most recent OPEN trades row for this symbol. exit_price
    may be None (price genuinely unavailable) -- realized_pnl/
    realized_ret_pct are then left null, same NULL-tolerant convention
    positions.realized_pnl already uses. No-ops (does nothing) if there's
    no open trade for this symbol -- callers that aren't sure one exists
    (e.g. reconciled_positions' fallback path) can call this unconditionally."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM trades WHERE symbol = ? AND status = 'open' "
        "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
    if row is None:
        conn.close()
        return
    today = dt.date.today().isoformat()
    holding_days = (dt.date.today() - dt.date.fromisoformat(row["entry_date"])).days
    realized_pnl = None
    realized_ret_pct = None
    if exit_price is not None:
        realized_pnl = (exit_price - row["entry_price"]) * row["qty"]
        realized_ret_pct = (exit_price / row["entry_price"] - 1) * 100
    conn.execute(
        "UPDATE trades SET status = 'closed', exit_date = ?, exit_price = ?, "
        "exit_reason = ?, realized_pnl = ?, realized_ret_pct = ?, "
        "holding_days = ? WHERE id = ?",
        (today, exit_price, exit_reason, realized_pnl, realized_ret_pct,
         holding_days, row["id"]))
    conn.commit()
    conn.close()


def get_trades(symbol: str | None = None, status: str | None = None,
              since: str | None = None) -> pd.DataFrame:
    """Also carries the position's CURRENT recommended_stop as
    latest_recommended_stop -- the trailing-stop value compute_stop_updates()
    recalculates daily (from the 08:30 rebalance scan) using that day's ATR,
    NOT yet necessarily pushed to the real broker GTT (see
    apply_stop_update()). Null for a closed trade, or a trade whose position
    was never linked (shouldn't happen for anything recorded through this
    app's own flows)."""
    conn = get_conn()
    where, params = [], []
    if symbol:
        where.append("t.symbol = ?")
        params.append(symbol)
    if status:
        where.append("t.status = ?")
        params.append(status)
    if since:
        where.append("t.entry_date >= ?")
        params.append(since)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    df = pd.read_sql(
        f"SELECT t.*, p.recommended_stop AS latest_recommended_stop "
        f"FROM trades t LEFT JOIN positions p ON t.position_id = p.id "
        f"{clause} ORDER BY t.entry_date DESC, t.id DESC",
        conn, params=params)
    conn.close()
    return df
