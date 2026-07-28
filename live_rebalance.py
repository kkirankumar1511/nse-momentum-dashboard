"""
Stage 1 of live automation: propose today's rebalance (sells + buys) by
running the exact same screener pipeline used live and in the backtest,
diffing it against your ACTUAL broker holdings.

This module NEVER places an order. It only computes a proposal and writes
it to disk for review -- you place orders yourself (Trade tab or broker
app). Meant to be run once a day, either from the dashboard's "Daily
Rebalance" tab or scheduled externally (Windows Task Scheduler / cron)
via `python live_rebalance.py`.

Why sells can lag a day: the rebalance rule (200 EMA / rank) is only ever
evaluated when this runs, so if you don't run it on a given day, a stock
that broke down that day won't be flagged until you next run it. Stops are
NOT covered here if you placed a GTT stop-loss at entry (kite_client.
place_gtt_stoploss) -- that already protects you intraday without needing
this job to run. This job only proposes the rebalance-rule exits/entries.
"""
from __future__ import annotations

import datetime as dt
import os

import pandas as pd

import config
import indicators
import kite_client
import screener
import state_db

LOG_PATH = os.path.join("cache", "live_rebalance_log.txt")


def compute_stop_updates(held_symbols: set[str], cfg: dict) -> list[dict]:
    """Recomputes each held position's trailing stop using the exact same
    formula as backtest.py's step 1b (highest_close_since_entry -
    trailing_atr_multiple*ATR, ratchet up only) -- so live and backtest
    logic can never quietly drift apart. Proposal-only: nothing is
    modified here, this only returns candidate updates for the dashboard's
    explicit-approval flow. highest_close/current_stop are persisted back
    to state_db regardless of whether anything ratchets this run, since
    that bookkeeping needs to continue every day."""
    stale = state_db.get_stale_open_symbols(held_symbols)
    exit_prices = kite_client.get_ltp(stale) if stale else {}
    positions = state_db.reconciled_positions(held_symbols, exit_prices)
    updates = []
    for sym, pos in positions.items():
        try:
            df = kite_client.fetch_daily_candles(sym, days=300)
        except Exception:
            continue
        if df.empty:
            continue
        today_close = float(df["close"].iloc[-1])
        highest_close = max(pos["highest_close"], today_close)
        atr_now = float(indicators.atr(df, cfg["atr_period"]).iloc[-1])
        new_stop = highest_close - cfg["trailing_atr_multiple"] * atr_now
        if new_stop > pos["current_stop"]:
            updates.append({
                "symbol": sym, "qty": pos["qty"],
                "current_stop": round(pos["current_stop"], 2),
                "recommended_stop": round(new_stop, 2),
                "gtt_trigger_id": pos.get("gtt_trigger_id"),
            })
        state_db.update_position_stop(sym, highest_close, new_stop)
    return updates


def get_live_holdings() -> pd.DataFrame:
    """Combined CNC holdings + any same-day positions, one row per symbol
    (qty, avg entry price), indexed by tradingsymbol. Delivery momentum
    swings mostly live in holdings; positions covers a same-day buy before
    it settles into holdings overnight."""
    frames = []
    pos = kite_client.get_positions()
    if not pos.empty and "quantity" in pos.columns:
        p = pos[pos["quantity"] != 0][["tradingsymbol", "quantity", "average_price"]]
        frames.append(p)
    hold = kite_client.get_holdings()
    if not hold.empty and "quantity" in hold.columns:
        h = hold[hold["quantity"] > 0][["tradingsymbol", "quantity", "average_price"]]
        frames.append(h)
    if not frames:
        return pd.DataFrame(columns=["quantity", "average_price"])
    combined = pd.concat(frames, ignore_index=True)
    combined["cost"] = combined["quantity"] * combined["average_price"]
    grouped = combined.groupby("tradingsymbol").agg(
        quantity=("quantity", "sum"), cost=("cost", "sum"))
    grouped["average_price"] = grouped["cost"] / grouped["quantity"]
    return grouped[["quantity", "average_price"]]


def _holdings_value(held: pd.DataFrame, ranked: pd.DataFrame) -> float:
    """Current mark-to-market value of held positions -- held's own
    average_price is COST basis, not current price, so this isn't just
    (quantity * average_price). Reuses ranked's already-fetched price where
    the held symbol appears there (the common case); falls back to a fresh
    LTP fetch only for the rest (delisted from F&O, gate failures, etc.),
    and to cost basis as a last resort if even that fails."""
    if held.empty:
        return 0.0
    prices = {sym: float(ranked.loc[sym, "price"])
             for sym in held.index if sym in ranked.index}
    missing = [sym for sym in held.index if sym not in prices]
    if missing:
        try:
            prices.update(kite_client.get_ltp(missing))
        except Exception:
            pass
    return float(sum(held.loc[sym, "quantity"] * prices.get(sym, held.loc[sym, "average_price"])
                     for sym in held.index))


def propose_rebalance(available_cash: float, cfg: dict | None = None,
                      fundamentals: pd.DataFrame | None = None,
                      progress_cb=None) -> dict:
    """Returns {"run_time", "sells", "buys", "holdings", "open_slots"}.
    Nothing here executes an order -- see module docstring."""
    def report(stage, frac):
        if progress_cb:
            progress_cb(stage, frac)
    cfg = dict(cfg or config.STRATEGY)

    report("Loading current holdings...", 0.05)
    held = get_live_holdings()

    report("Scanning universe (screener pipeline)...", 0.10)
    ranked = screener.run_screen(
        with_fundamentals=True, fundamentals=fundamentals,
        progress_cb=lambda s, f: report(s, 0.10 + f * 0.7))

    candidates = ranked[ranked["all_gates"]]
    keep_zone = set(candidates.head(cfg["max_positions"] * 2).index)

    # ---- Sells: same rebalance rule as the backtest (200 EMA / rank) ----
    report("Checking held positions against the rebalance rule...", 0.85)
    sells = []
    for sym, row in held.iterrows():
        r = ranked.loc[sym] if sym in ranked.index else None
        if r is None:
            sells.append({"symbol": sym, "qty": int(row["quantity"]),
                         "avg_price": float(row["average_price"]),
                         "reason": "no data / not in current universe"})
        elif not bool(r.get("above_ema200", False)):
            sells.append({"symbol": sym, "qty": int(row["quantity"]),
                         "avg_price": float(row["average_price"]),
                         "reason": "closed below 200 EMA"})
        elif sym not in keep_zone:
            sells.append({"symbol": sym, "qty": int(row["quantity"]),
                         "avg_price": float(row["average_price"]),
                         "reason": f"dropped out of top {cfg['max_positions'] * 2} rank"})
    sells_df = pd.DataFrame(sells)

    # ---- Buys: fill slots opened up by the sells above ----
    report("Sizing new candidates...", 0.92)
    sold_syms = set(sells_df["symbol"]) if not sells_df.empty else set()
    still_held = set(held.index) - sold_syms
    open_slots = max(cfg["max_positions"] - len(still_held), 0)

    # Equal-weight capital allocation, not the risk-based screener.position_size()
    # backtest.py uses: per-trade capital = total account equity / max_positions
    # (so it scales with your capital and max_positions setting directly), capped
    # by what's actually left in cash divided across the slots still to fill this
    # run -- see screener.capital_position_size()'s docstring for the full reasoning.
    total_equity = available_cash + _holdings_value(held, ranked)
    remaining_cash = available_cash

    buys = []
    if open_slots > 0:
        for sym, row in candidates.iterrows():
            if len(buys) >= open_slots:
                break
            if sym in still_held:
                continue
            price = float(row["price"])
            stop = float(row["suggested_stop"])
            slots_remaining = open_slots - len(buys)
            qty = screener.capital_position_size(
                total_equity, remaining_cash, price, slots_remaining, cfg["max_positions"])
            if qty <= 0:
                continue
            remaining_cash -= qty * price
            fscore = row.get("fundamental_score")
            buys.append({
                "symbol": sym, "qty": qty, "price": round(price, 2),
                "stop": round(stop, 2), "score": float(row["score"]),
                "fundamental_score": None if pd.isna(fscore) else round(float(fscore), 1),
                "fundamental_rubric": row.get("fundamental_rubric"),
            })
    buys_df = pd.DataFrame(buys)

    # ---- Trailing-stop updates: recommend, never modify a live GTT here.
    # Uses currently ACTUALLY held symbols, not "still_held" (which already
    # subtracts today's proposed-but-not-yet-executed sells) -- a position
    # you haven't gotten around to selling yet still needs its stop tracked.
    report("Checking trailing-stop levels...", 0.97)
    stop_updates = compute_stop_updates(set(held.index), cfg)
    stop_updates_df = pd.DataFrame(stop_updates)

    report("Done", 1.0)
    result = {
        "run_time": dt.datetime.now(),
        "sells": sells_df,
        "buys": buys_df,
        "stop_updates": stop_updates_df,
        "holdings": held.reset_index().rename(columns={"tradingsymbol": "symbol"}),
        "open_slots": open_slots,
    }
    state_db.save_rebalance_run(result)
    return result


def main():
    """Run headless, e.g. from Windows Task Scheduler -- see README's
    "Scheduled scan" section. Every run appends a timestamped block to
    LOG_PATH regardless of outcome (including a Kite-auth failure), since a
    scheduled run has no console to watch -- this is the only record of
    whether today's run happened and what it found."""
    os.makedirs("cache", exist_ok=True)
    log_lines = [f"\n{'=' * 60}\n{dt.datetime.now():%d %b %Y %H:%M:%S}\n{'=' * 60}"]

    def log(msg=""):
        print(msg)
        log_lines.append(msg)

    try:
        margins = kite_client.get_margins()
        available_cash = margins["equity"]["available"]["live_balance"]
    except Exception as e:
        log(f"FAILED -- Kite connection failed (token may have expired): {e}")
        log("Refresh the token (python kite_client.py login / token <request_token>) "
           "before the next scheduled run, or run manually from the dashboard.")
        state_db.save_rebalance_failure(str(e))
        with open(LOG_PATH, "a") as f:
            f.write("\n".join(log_lines) + "\n")
        return

    def cb(stage, frac):
        pass  # progress bar text is meaningless in a headless/logged run

    result = propose_rebalance(available_cash, progress_cb=cb)

    log(f"Rebalance proposal ({result['run_time']:%d %b %Y %H:%M})")
    log(f"Open slots: {result['open_slots']}")
    log("\n-- Proposed SELLS --")
    log(result["sells"].to_string(index=False) if not result["sells"].empty
       else "(none)")
    log("\n-- Proposed BUYS --")
    log(result["buys"].to_string(index=False) if not result["buys"].empty
       else "(none)")
    log("\n-- Recommended stop updates (trailing stop) --")
    log(result["stop_updates"].to_string(index=False) if not result["stop_updates"].empty
       else "(none)")
    log(f"\nSaved to {state_db.DB_PATH}")
    log("Nothing was placed or modified -- review and execute/apply manually "
       "in the dashboard's Live Rebalance page or the Trade tab.")
    with open(LOG_PATH, "a") as f:
        f.write("\n".join(log_lines) + "\n")


if __name__ == "__main__":
    main()
