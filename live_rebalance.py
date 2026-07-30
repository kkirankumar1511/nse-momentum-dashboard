"""
Stage 1 of live automation: propose today's rebalance (sells + buys) by
running the exact same screener pipeline used live and in the backtest,
diffing it against your ACTUAL broker holdings.

propose_rebalance()/main() NEVER place an order -- they only compute a
proposal and write it to disk for review, you place orders yourself (Trade
tab or broker app). Meant to be run once a day, either from the dashboard's
"Daily Rebalance" tab or scheduled externally (Windows Task Scheduler /
cron / trading_service.py) via `python live_rebalance.py`.

check_gap_down_stops()/main_gap_check() are the one deliberate exception --
see that function's docstring for why an immediate automatic market sell is
the right call there specifically, unlike everywhere else in this module.

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
        state_db.update_position_stop(sym, highest_close, atr_now, new_stop)
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


def get_unsettled_quantities() -> dict[str, int]:
    """Per-symbol quantity that would NOT yield usable cash today if sold --
    same-day CNC positions (bought today, shows in kite_client.get_positions()
    until it settles into holdings overnight) plus T1 holdings
    (kite_client.get_holdings()'s t1_quantity -- bought yesterday, settlement
    completes the day after that). Zerodha credits a SOLD, already-settled
    (T0) holding's proceeds as usable margin the same day; a same-day
    position or T1 holding's sale proceeds only become usable once that
    settlement cycle finishes -- this is what propose_rebalance() checks
    before counting a proposed sell's proceeds toward today's buying
    power (the "T1/BTST" case). get_holdings() itself folds t1_quantity
    into quantity for DISPLAY purposes (see its docstring) -- the raw
    t1_quantity column is still present on the DataFrame it returns, just
    not documented as something callers should look at; this is the one
    place that does."""
    unsettled: dict[str, int] = {}
    pos = kite_client.get_positions()
    if not pos.empty and "quantity" in pos.columns:
        for _, r in pos[pos["quantity"] > 0].iterrows():
            sym = r["tradingsymbol"]
            unsettled[sym] = unsettled.get(sym, 0) + int(r["quantity"])
    hold = kite_client.get_holdings()
    if not hold.empty and "t1_quantity" in hold.columns:
        for _, r in hold[hold["t1_quantity"] > 0].iterrows():
            sym = r["tradingsymbol"]
            unsettled[sym] = unsettled.get(sym, 0) + int(r["t1_quantity"])
    return unsettled


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

    # Cache the same ranked table the Screener page's own "Run screen"
    # button computes -- a rebalance run already does this full scan
    # internally, so caching it here too means the Screener page shows
    # this run's fresh data without a second, redundant universe fetch.
    os.makedirs("cache", exist_ok=True)
    ranked.to_pickle(screener.SCREEN_CACHE)

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
            keep_zone_size = cfg["max_positions"] * 2
            if sym in candidates.index:
                rank = candidates.index.get_loc(sym) + 1
                reason = (f"dropped out of top {keep_zone_size} rank "
                         f"(now #{rank} of {len(candidates)})")
            else:
                reason = (f"failed a technical gate (trend/near-high/RSI) -- "
                         f"not in the top {keep_zone_size} at all")
            sells.append({"symbol": sym, "qty": int(row["quantity"]),
                         "avg_price": float(row["average_price"]), "reason": reason})
    sells_df = pd.DataFrame(sells)

    # ---- Buys: fill slots opened up by the sells above ----
    report("Sizing new candidates...", 0.92)
    sold_syms = set(sells_df["symbol"]) if not sells_df.empty else set()
    still_held = set(held.index) - sold_syms
    open_slots = max(cfg["max_positions"] - len(still_held), 0)

    # Total portfolio value (cash + everything held, current prices) --
    # the equal-weight denominator, unchanged either sizing mode.
    total_equity = available_cash + _holdings_value(held, ranked)
    target_per_slot = total_equity / cfg["max_positions"]

    # Sell proceeds use each symbol's CURRENT price (from the screener,
    # today's close), not the cost-basis avg_price the sells table
    # displays -- avg_price would understate/overstate what's actually
    # about to come back as cash. Only the SETTLED portion of each sale
    # counts toward today's usable pool -- a same-day position or T1
    # holding (bought yesterday, BTST) still needs its own settlement
    # cycle before Zerodha treats its sale proceeds as usable margin; see
    # get_unsettled_quantities()'s docstring.
    unsettled_qtys = get_unsettled_quantities()
    sell_proceeds = 0.0
    unsettled_proceeds = 0.0
    for sym, qty, avg_price in zip(
            sells_df.get("symbol", []), sells_df.get("qty", []),
            sells_df.get("avg_price", [])):
        price = float(ranked.loc[sym, "price"]) if sym in ranked.index else avg_price
        unsettled_qty = min(qty, unsettled_qtys.get(sym, 0))
        settled_qty = qty - unsettled_qty
        sell_proceeds += price * settled_qty
        unsettled_proceeds += price * unsettled_qty
    cash_pool = available_cash + sell_proceeds

    buys = []
    top_ups = []
    open_positions_now = state_db.get_open_positions()
    if cfg.get("advanced_equal_weight_sizing", True):
        # New candidates (not held), rank order, plus held-but-still-
        # gate-passing symbols (eligible for topping up, not for a fresh
        # entry -- allocate_equal_weight_buys() filters that itself).
        new_candidate_syms = [sym for sym in candidates.index if sym not in still_held]
        held_still_candidates = [sym for sym in candidates.index if sym in still_held]
        allocator_syms = new_candidate_syms + held_still_candidates
        prices = {sym: float(candidates.loc[sym, "price"]) for sym in allocator_syms}
        held_info = {sym: (int(held.loc[sym, "quantity"]), prices[sym])
                    for sym in held_still_candidates}
        alloc = screener.allocate_equal_weight_buys(
            allocator_syms, prices, held_info, cash_pool=cash_pool,
            total_equity=total_equity, max_positions=cfg["max_positions"],
            tolerance_pct=cfg.get("equal_weight_tolerance_pct", 0.20))

        for sym, (qty, size_reason) in alloc["new_buys"].items():
            row = candidates.loc[sym]
            price = float(row["price"])
            stop = float(row["suggested_stop"])
            fscore = row.get("fundamental_score")
            fscore = None if pd.isna(fscore) else round(float(fscore), 1)
            rank = candidates.index.get_loc(sym) + 1
            reason = (f"Ranked #{rank} of {len(candidates)} momentum candidates "
                     f"(score {row['score']:.2f}); RSI {row['rsi']:.0f}, "
                     f"{row['pct_52w_high'] * 100:.0f}% of 52w high, "
                     f"vol expansion {row['vol_expansion']:.2f}x")
            if fscore is not None:
                reason += f"; fundamental score {fscore:.0f}/100"
            if size_reason:
                reason += f" -- {size_reason}"
            buys.append({
                "symbol": sym, "qty": qty, "price": round(price, 2),
                "stop": round(stop, 2), "score": float(row["score"]),
                "rsi": float(row["rsi"]), "pct_52w_high": float(row["pct_52w_high"]),
                "vol_expansion": float(row["vol_expansion"]),
                "fundamental_score": fscore,
                "fundamental_rubric": row.get("fundamental_rubric"),
                "reason": reason,
            })
        for sym, extra_qty in alloc["top_ups"].items():
            pos = open_positions_now.get(sym)
            top_ups.append({
                "symbol": sym, "extra_qty": extra_qty,
                "price": round(prices[sym], 2),
                "gtt_trigger_id": pos.get("gtt_trigger_id") if pos else None,
            })
    elif open_slots > 0:
        # Rollback path: original one-symbol-at-a-time greedy fill.
        remaining_cash = available_cash
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
            fscore = None if pd.isna(fscore) else round(float(fscore), 1)
            rank = candidates.index.get_loc(sym) + 1
            reason = (f"Ranked #{rank} of {len(candidates)} momentum candidates "
                     f"(score {row['score']:.2f}); RSI {row['rsi']:.0f}, "
                     f"{row['pct_52w_high'] * 100:.0f}% of 52w high, "
                     f"vol expansion {row['vol_expansion']:.2f}x")
            if fscore is not None:
                reason += f"; fundamental score {fscore:.0f}/100"
            buys.append({
                "symbol": sym, "qty": qty, "price": round(price, 2),
                "stop": round(stop, 2), "score": float(row["score"]),
                "rsi": float(row["rsi"]), "pct_52w_high": float(row["pct_52w_high"]),
                "vol_expansion": float(row["vol_expansion"]),
                "fundamental_score": fscore,
                "fundamental_rubric": row.get("fundamental_rubric"),
                "reason": reason,
            })
    buys_df = pd.DataFrame(buys)
    top_ups_df = pd.DataFrame(top_ups)

    # How much MORE cash would be needed to fully equal-weight every open
    # slot and every under-target held position at once -- shown on the
    # UI so you know exactly how much to add, rather than silently getting
    # partial fills or a stopped proposal with no explanation.
    cash_needed = open_slots * target_per_slot
    for sym in still_held:
        if sym in candidates.index:
            current_value = float(held.loc[sym, "quantity"]) * float(candidates.loc[sym, "price"])
            if current_value < target_per_slot:
                cash_needed += target_per_slot - current_value
    cash_shortfall = max(0.0, cash_needed - cash_pool)

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
        "top_ups": top_ups_df,
        "stop_updates": stop_updates_df,
        "holdings": held.reset_index().rename(columns={"tradingsymbol": "symbol"}),
        "open_slots": open_slots,
        "screen_candidates": len(ranked),
        "screen_gate_passers": int(ranked["all_gates"].sum()),
        "target_per_slot": round(target_per_slot, 2),
        "cash_pool": round(cash_pool, 2),
        "cash_needed_for_full_equal_weight": round(cash_needed, 2),
        "cash_shortfall": round(cash_shortfall, 2),
        "unsettled_proceeds": round(unsettled_proceeds, 2),
    }
    state_db.save_rebalance_run(result)
    return result


def check_gap_down_stops() -> list[dict]:
    """Morning safety net -- meant to run once, shortly after market open.

    kite_client.place_gtt_stoploss's triggered order is a LIMIT sell at
    trigger_price*0.995, not a market order. That's fine for an ordinary
    intraday stop hit, but if a stock gaps down hard overnight -- opening
    already well below the stop -- the GTT still "triggers" (LTP crossed the
    threshold) but the resulting limit order can sit unfilled if the market
    price has already gapped past it, leaving the position open with no
    actual exit. This checks every open position's LTP against its tracked
    stop and, for anything already through it, places an immediate MARKET
    sell (via kite_client.square_off_position, the same call the dashboard's
    manual Square-off button uses) to guarantee an exit, then deletes the
    now-stale GTT so it can't also sit there confusingly pointed at a
    position that no longer exists.

    This is the one place in this module that places a real order
    automatically, without a human confirmation step -- deliberately, since
    the whole point is to react before a human could realistically check
    and confirm; propose_rebalance()/main() stay proposal-only everywhere
    else.

    Returns one {"symbol", "qty", "stop", "ltp", "order_id", "gtt_deleted",
    "error"} dict per position that was gapped below its stop (empty list
    if nothing needed exiting)."""
    positions = state_db.get_open_positions()
    if not positions:
        return []

    try:
        ltps = kite_client.get_ltp(list(positions.keys()))
    except Exception as e:
        return [{"symbol": None, "error": f"Could not fetch LTPs: {e}"}]

    gtts = kite_client.get_active_gtts()
    gtt_by_symbol = {}
    if not gtts.empty and "condition" in gtts.columns:
        for _, row in gtts.iterrows():
            cond = row["condition"]
            sym = cond.get("tradingsymbol") if isinstance(cond, dict) else None
            if sym:
                gtt_by_symbol[sym] = row["id"]

    actions = []
    still_held = set(positions.keys())
    for sym, pos in positions.items():
        ltp = ltps.get(sym)
        if ltp is None or ltp > pos["current_stop"]:
            continue  # not gapped below stop -- nothing to do

        action = {"symbol": sym, "qty": pos["qty"], "stop": pos["current_stop"],
                  "ltp": ltp, "order_id": None, "gtt_deleted": False, "error": None}
        try:
            action["order_id"] = kite_client.square_off_position(sym)
            still_held.discard(sym)
            state_db.close_trade(sym, ltp, "gap_down_stop")
        except Exception as e:
            action["error"] = f"Market sell FAILED: {e}"
            actions.append(action)
            continue

        gtt_id = gtt_by_symbol.get(sym)
        if gtt_id:
            try:
                kite_client.delete_gtt(gtt_id)
                action["gtt_deleted"] = True
            except Exception as e:
                action["error"] = f"Sold OK but GTT delete failed: {e}"

        actions.append(action)

    if actions:
        exit_prices = {a["symbol"]: a["ltp"] for a in actions if a.get("order_id")}
        state_db.reconciled_positions(still_held, exit_prices)

    return actions


def main_gap_check():
    """Headless entry point for check_gap_down_stops() -- e.g. from
    trading_service.py's scheduler, shortly after market open. Logs to the
    same LOG_PATH as main()'s rebalance scan, since both are automated runs
    with no console to watch. Also wrapped in state_db.job_run() -- see its
    docstring -- so the Job Log page has a persisted, filterable record on
    top of the plain-text tail log."""
    os.makedirs("cache", exist_ok=True)
    log_lines = [f"\n{'=' * 60}\n{dt.datetime.now():%d %b %Y %H:%M:%S} (gap-down check)"
                f"\n{'=' * 60}"]

    def log(msg=""):
        print(msg)
        log_lines.append(msg)

    with state_db.job_run("gap_check", "scheduled") as jr:
        try:
            actions = check_gap_down_stops()
        except Exception as e:
            log(f"FAILED -- {e}")
            with open(LOG_PATH, "a") as f:
                f.write("\n".join(log_lines) + "\n")
            raise

        if not actions:
            log("No positions gapped below their stop.")
        for a in actions:
            if a.get("error") and a.get("order_id") is None:
                log(f"⚠️ {a['symbol']}: {a['error']}")
            else:
                log(f"🔴 {a['symbol']}: gapped to ₹{a['ltp']:.2f} (stop ₹{a['stop']:.2f}) -- "
                   f"market SELL order {a['order_id']}, GTT deleted: {a['gtt_deleted']}"
                   + (f" -- {a['error']}" if a.get("error") else ""))
        with open(LOG_PATH, "a") as f:
            f.write("\n".join(log_lines) + "\n")
        jr["summary"] = (f"{len(actions)} gap action(s)" if actions
                        else "no positions gapped below stop")


def main():
    """Run headless, e.g. from Windows Task Scheduler -- see README's
    "Scheduled scan" section. Every run appends a timestamped block to
    LOG_PATH regardless of outcome (including a Kite-auth failure), since a
    scheduled run has no console to watch -- this is the only record of
    whether today's run happened and what it found. Also wrapped in
    state_db.job_run() -- see its docstring -- so the Job Log page has a
    persisted, filterable record on top of the plain-text tail log; a
    Kite-auth failure now also propagates (after logging) instead of
    silently returning, so the systemd unit itself shows failed too."""
    os.makedirs("cache", exist_ok=True)
    log_lines = [f"\n{'=' * 60}\n{dt.datetime.now():%d %b %Y %H:%M:%S}\n{'=' * 60}"]

    def log(msg=""):
        print(msg)
        log_lines.append(msg)

    with state_db.job_run("rebalance_scan", "scheduled") as jr:
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
            raise

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
        log("\n-- Proposed TOP-UPS --")
        log(result["top_ups"].to_string(index=False) if not result["top_ups"].empty
           else "(none)")
        log("\n-- Recommended stop updates (trailing stop) --")
        log(result["stop_updates"].to_string(index=False) if not result["stop_updates"].empty
           else "(none)")
        log(f"\nEqual-weight target: Rs.{result['target_per_slot']:,.0f}/slot, "
           f"Rs.{result['cash_pool']:,.0f} available (cash + proposed sell proceeds)")
        if result["unsettled_proceeds"] > 0:
            log(f"Rs.{result['unsettled_proceeds']:,.0f} of today's sell proceeds is "
               "from a same-day position or T1 (BTST) holding -- not usable until "
               "settlement completes (excluded from the available figure above).")
        if result["cash_shortfall"] > 0:
            log(f"SHORTFALL: Rs.{result['cash_shortfall']:,.0f} more needed to fully "
               "equal-weight every open slot and under-target holding")
        else:
            log("Sufficient cash to fully equal-weight every open slot and "
               "under-target holding.")
        log(f"\nSaved to {state_db.DB_PATH}")
        log("Nothing was placed or modified -- review and execute/apply manually "
           "in the dashboard's Live Rebalance page or the Trade tab.")
        with open(LOG_PATH, "a") as f:
            f.write("\n".join(log_lines) + "\n")
        jr["summary"] = (f"{len(result['buys'])} buys, {len(result['sells'])} sells, "
                        f"{len(result['stop_updates'])} stop updates")

    # propose_rebalance() already ran the full screener pipeline and cached
    # it to screener.SCREEN_CACHE above -- log this as its own screen_run
    # entry too (not folded into the rebalance_scan entry above) so the Job
    # Log's "last run" for screen_run also reflects this time, even though
    # no separate screener pass actually happened.
    sr_id = state_db.start_job_run("screen_run", "scheduled")
    state_db.finish_job_run(
        sr_id, "success",
        summary=(f"{result['screen_candidates']} candidates "
                f"({result['screen_gate_passers']} passing all gates) -- "
                "refreshed alongside the rebalance scan"))


if __name__ == "__main__":
    import sys
    if "--gap-check" in sys.argv:
        main_gap_check()
    else:
        main()
