"""
Isolated SQLite state for the experimental Heikin-Ashi paper-trading
strategy -- completely separate from the REAL account's cache/state.db.

Reuses state_db's schema/connection machinery (state_db.get_conn accepts a
db_path override specifically for this purpose -- see its docstring) but
NEVER imports anything beyond that: no kite_client, no live_rebalance, no
order-placement path is reachable from this module at all. Every accessor
here is a thin wrapper that opens ITS OWN connection to PAPER_DB_PATH,
mirroring the shape of state_db's real-account accessors
(record_new_position/record_trade_entry/close_position/close_trade/
update_position_stop) but writing to positions/trades rows that never
represent real capital.

Local-only, gitignored (same as the whole cache/ dir) -- never meant to
leave this machine, same convention as state_db.py itself.
"""
from __future__ import annotations

import datetime as dt
import os

import pandas as pd

import state_db

PAPER_DB_PATH = os.path.join("cache", "state_paper.db")
STARTING_CAPITAL = 1_000_000.0


def _conn():
    """The ONLY place this module opens a connection. Asserts on every call
    (not just at import) that the resolved path can never be the real
    state DB -- catches a later `state_db.DB_PATH = ...` reassignment
    (trading_service.py and scripts/run_sandbox.py both do this), not just
    a mistake made once at import time."""
    real = os.path.abspath(state_db.DB_PATH)
    paper = os.path.abspath(PAPER_DB_PATH)
    if real == paper:
        raise RuntimeError(f"paper_db refusing to open the REAL state DB: {paper}")
    # Pattern check, not an exact-filename match -- allows a throwaway
    # variant (e.g. state_paper_VALIDATION.db, used by the backfill
    # validation script) while still catching the actual danger case:
    # PAPER_DB_PATH ever being pointed at "state.db" (the real DB's name).
    basename = os.path.basename(paper).lower()
    if "paper" not in basename or basename == "state.db":
        raise RuntimeError(f"unexpected paper DB filename: {paper}")
    conn = state_db.get_conn(paper)
    _ensure_paper_schema(conn)
    return conn


def _ensure_paper_schema(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS paper_account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            starting_capital REAL NOT NULL,
            cash REAL NOT NULL,
            started_on TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS paper_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_date TEXT NOT NULL UNIQUE,
            run_time TEXT NOT NULL,
            status TEXT NOT NULL,
            n_watchlist INTEGER,
            n_entries INTEGER,
            n_exits INTEGER,
            cash REAL,
            equity REAL,
            watchlist_json TEXT,
            error_message TEXT
        );
    """)
    conn.commit()


def ensure_initialised(starting_capital: float = STARTING_CAPITAL) -> None:
    """Seeds the paper_account singleton (and a matching cash_flows entry,
    for XIRR-style math if ever wanted) on first use. No-ops if already
    initialised. Defensively inserts a sentinel equity_log row too --
    state_db.get_conn's _migrate_equity_log_once runs on EVERY open
    (including this module's very first) and would import cache/
    equity_log.csv's full history into a fresh equity_log table if that
    file ever exists; a non-empty equity_log makes that migration's own
    "existing rows" guard a no-op forever, closing the hole before it can
    matter (verified this file doesn't exist on this machine today, but
    the guard costs one INSERT and removes the failure mode entirely)."""
    conn = _conn()
    row = conn.execute("SELECT * FROM paper_account WHERE id = 1").fetchone()
    if row is not None:
        conn.close()
        return
    today = dt.date.today().isoformat()
    conn.execute(
        "INSERT INTO paper_account (id, starting_capital, cash, started_on) "
        "VALUES (1, ?, ?, ?)", (starting_capital, starting_capital, today))
    conn.execute(
        "INSERT INTO cash_flows (date, amount, note) VALUES (?, ?, ?)",
        (today, starting_capital, "Paper trading account opened"))
    conn.execute(
        "INSERT OR IGNORE INTO equity_log (date, value) VALUES (?, ?)",
        (today, starting_capital))
    conn.commit()
    conn.close()


def get_cash() -> float:
    conn = _conn()
    row = conn.execute("SELECT cash FROM paper_account WHERE id = 1").fetchone()
    conn.close()
    return float(row["cash"]) if row else STARTING_CAPITAL


def set_cash(value: float) -> None:
    conn = _conn()
    conn.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (value,))
    conn.commit()
    conn.close()


def get_open_positions() -> dict[str, dict]:
    """Each row also carries initial_stop (pulled from the matching open
    trades row) alongside positions.current_stop -- the day-loop needs
    both every day to tell, at exit time, whether the stop that caught a
    position was still its untouched initial value or had ratcheted."""
    conn = _conn()
    rows = conn.execute("""
        SELECT p.*, t.initial_stop AS initial_stop
        FROM positions p
        LEFT JOIN trades t ON t.position_id = p.id AND t.status = 'open'
        WHERE p.status = 'open'
    """).fetchall()
    conn.close()
    return {r["symbol"]: dict(r) for r in rows}


def open_position(symbol: str, date: str, entry_price: float, qty: int,
                  stop: float, snapshot: dict, trigger_type: str) -> None:
    """date: an explicit ISO date (the DATA date the trigger fired on, not
    necessarily wall-clock today) -- unlike state_db.record_new_position/
    record_trade_entry, which hardcode dt.date.today(). Explicit dating is
    required for catch-up replay of missed days to produce historically
    correct entry_date values instead of every backfilled trade showing
    today's date."""
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO positions (symbol, entry_date, entry_price, qty, "
        "highest_close, current_stop, gtt_trigger_id, status) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, 'open')",
        (symbol, date, entry_price, qty, entry_price, stop))
    position_id = cur.lastrowid
    conn.execute(
        "INSERT INTO trades (position_id, symbol, entry_date, entry_price, "
        "qty, initial_stop, entry_score, entry_rsi, entry_pct_52w_high, "
        "entry_vol_expansion, entry_fundamental_score, entry_reason, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
        (position_id, symbol, date, entry_price, qty, stop,
         snapshot.get("score"), snapshot.get("rsi"),
         snapshot.get("pct_52w_high"), snapshot.get("vol_expansion"),
         snapshot.get("fundamental_score"),
         f"{trigger_type} trigger; {snapshot.get('entry_reason', '')}".strip("; ")))
    conn.commit()
    conn.close()


def close_position(symbol: str, date: str, exit_price: float, reason: str,
                   stop_type: str) -> None:
    """date: explicit ISO date, same reasoning as open_position. Closes
    BOTH the positions row and the matching open trades row in one
    connection/transaction -- deliberately not two separate calls like
    state_db's record_new_position+record_trade_entry pairing, which is
    what created a phantom-row hazard there (a position row could exist
    with no matching trade row if the second call failed); one connection
    here removes that failure mode entirely for this isolated feature.
    stop_type ('initial' | 'trailing') is appended to entry_reason via
    exit_reason since the trades schema has no dedicated column for it."""
    conn = _conn()
    pos = conn.execute(
        "SELECT * FROM positions WHERE symbol = ? AND status = 'open'",
        (symbol,)).fetchone()
    if pos is None:
        conn.close()
        return
    conn.execute(
        "UPDATE positions SET status = 'closed', closed_date = ?, "
        "exit_price = ?, realized_pnl = ? WHERE id = ?",
        (date, exit_price, (exit_price - pos["entry_price"]) * pos["qty"], pos["id"]))
    trade = conn.execute(
        "SELECT * FROM trades WHERE symbol = ? AND status = 'open' "
        "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
    if trade is not None:
        holding_days = (dt.date.fromisoformat(date)
                        - dt.date.fromisoformat(trade["entry_date"])).days
        realized_pnl = (exit_price - trade["entry_price"]) * trade["qty"]
        realized_ret_pct = (exit_price / trade["entry_price"] - 1) * 100
        conn.execute(
            "UPDATE trades SET status = 'closed', exit_date = ?, exit_price = ?, "
            "exit_reason = ?, realized_pnl = ?, realized_ret_pct = ?, "
            "holding_days = ? WHERE id = ?",
            (date, exit_price, f"{reason} ({stop_type})", realized_pnl,
             realized_ret_pct, holding_days, trade["id"]))
    conn.commit()
    conn.close()


def ratchet_stop(symbol: str, date: str, new_stop: float, highest_close: float,
                 atr_value: float) -> None:
    """Only ever called with new_stop > current stop (ratchet-up-only is
    enforced by the caller, matching backtest_triggered.py's convention) --
    always logs to stop_update_log for the daily-history record, same
    table shape state_db's real update_position_stop uses."""
    conn = _conn()
    pos = conn.execute(
        "SELECT * FROM positions WHERE symbol = ? AND status = 'open'",
        (symbol,)).fetchone()
    if pos is None:
        conn.close()
        return
    conn.execute(
        "UPDATE positions SET highest_close = ?, current_stop = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (highest_close, new_stop, pos["id"]))
    conn.execute(
        "INSERT INTO stop_update_log (position_id, date, old_stop, new_stop, applied) "
        "VALUES (?, ?, ?, ?, 1)", (pos["id"], date, pos["current_stop"], new_stop))
    conn.commit()
    conn.close()


def log_equity(date: str, value: float) -> None:
    """Explicit date (unlike state_db.log_equity_snapshot, which hardcodes
    dt.date.today()) -- required so catch-up replay of missed days writes
    the correct historical equity curve, not a pile of rows all dated
    today."""
    conn = _conn()
    conn.execute(
        "INSERT INTO equity_log (date, value) VALUES (?, ?) "
        "ON CONFLICT(date) DO UPDATE SET value = excluded.value", (date, value))
    conn.commit()
    conn.close()


def get_trades() -> pd.DataFrame:
    conn = _conn()
    df = pd.read_sql_query("SELECT * FROM trades ORDER BY entry_date DESC, id DESC", conn)
    conn.close()
    return df


def get_equity_log() -> pd.DataFrame:
    conn = _conn()
    df = pd.read_sql_query("SELECT * FROM equity_log ORDER BY date", conn)
    conn.close()
    return df


def record_scan(scan_date: str, status: str, n_watchlist: int = 0,
                n_entries: int = 0, n_exits: int = 0, cash: float = 0.0,
                equity: float = 0.0, watchlist_json: str | None = None,
                error_message: str | None = None) -> None:
    """scan_date UNIQUE is the idempotency key -- re-running the scan for a
    day already recorded is a no-op (INSERT OR REPLACE), so a double-fire
    (manual button + scheduled task both firing) can't double-trade."""
    conn = _conn()
    conn.execute(
        "INSERT INTO paper_scans (scan_date, run_time, status, n_watchlist, "
        "n_entries, n_exits, cash, equity, watchlist_json, error_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(scan_date) DO UPDATE SET run_time=excluded.run_time, "
        "status=excluded.status, n_watchlist=excluded.n_watchlist, "
        "n_entries=excluded.n_entries, n_exits=excluded.n_exits, "
        "cash=excluded.cash, equity=excluded.equity, "
        "watchlist_json=excluded.watchlist_json, "
        "error_message=excluded.error_message",
        (scan_date, dt.datetime.now().isoformat(), status, n_watchlist,
         n_entries, n_exits, cash, equity, watchlist_json, error_message))
    conn.commit()
    conn.close()


def last_scan_date() -> str | None:
    conn = _conn()
    row = conn.execute(
        "SELECT scan_date FROM paper_scans WHERE status = 'ok' "
        "ORDER BY scan_date DESC LIMIT 1").fetchone()
    conn.close()
    return row["scan_date"] if row else None


def get_scans(limit: int = 60) -> pd.DataFrame:
    conn = _conn()
    df = pd.read_sql_query(
        "SELECT * FROM paper_scans ORDER BY scan_date DESC LIMIT ?", conn, params=(limit,))
    conn.close()
    return df


def reset() -> None:
    """Deletes the paper DB file entirely -- the "abandon this experiment"
    button. The next _conn() call recreates it fresh via get_conn's
    idempotent schema script."""
    if os.path.exists(PAPER_DB_PATH):
        os.remove(PAPER_DB_PATH)
