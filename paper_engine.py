"""
Live, end-of-day paper trading for the Heikin-Ashi trend-following
strategy that this session's local backtesting validated (CAGR 20.00%,
alpha +9.32%, MaxDD -13.37%, Sharpe 1.53 over 8.34 years -- see
backtest_triggered.py / scripts/run_triggered_backtest_local.py).

PAPER_CFG below is copied VERBATIM from scripts/run_triggered_backtest_
local.py's FILTER_CFG_OVERRIDE + HEIKIN_ASHI_OVERRIDE (the exact winning
config) and frozen in code -- deliberately NOT read from the live-editable
config.STRATEGY/strategy_config DB table, so an unrelated Admin-page tweak
to the REAL live strategy can never silently change paper results and
break comparability with the validated backtest numbers.

Import whitelist IS the safety property -- grep this file for
kite_client/live_rebalance/screener/state_db and find nothing:
    rg -n "kite_client|live_rebalance|^import screener|^import state_db|
          place_order|place_gtt|modify_gtt|delete_gtt|square_off" paper_engine.py
Market data reaches this module only through backtest.py's existing
READ-ONLY functions (load_candles_cached/load_long_history_cached), which
do their own internal `import kite_client` for read endpoints only.
paper_db.py is the only state-writing dependency, and it only ever opens
cache/state_paper.db (see its own isolation assertion).

Day-loop logic below is a deliberate, hand-kept-in-sync PORT of
backtest_triggered.py's day loop (see that file's step 1/1b/2/2b/3 --
line numbers noted in comments below), NOT a shared/refactored function --
editing backtest_triggered.py to "share" the loop with this file would put
the already-validated backtest engine at risk of a silent behaviour
change for the sake of an experimental live feature. Only the branches
that are actually reachable under PAPER_CFG are ported (target-check and
institutional-specific exits are dead code under this config -- see
detect_trigger's docstring -- and are intentionally NOT ported).

Entries fill at the trigger's own price (today's real close, per
heikin_ashi_trend_entry). Stop-outs fill using the exact same gap-aware
rule the backtest uses (min(stop, high), further capped by open if the
day gapped below the stop) -- NOT at the close -- so paper results stay
directly comparable to the backtest that validated this strategy.

Local-only, experimental. Writes only to cache/state_paper.db. Never
placed a real order in its history.

Run with:
    python paper_engine.py                 # catch-up scan, real writes
    python paper_engine.py --dry-run        # compute + print, write nothing
    python paper_engine.py --date 2026-08-14  # force a specific single day
    python paper_engine.py --reset          # delete the paper DB and start over
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

import config
import indicators
import nse_holidays
import paper_db
import sector_universe as su
import trigger_indicators as ti
import trigger_strategy as ts
from backtest import (_apply_sector_cap, load_candles_cached,
                      load_long_history_cached, rank_universe_asof)

FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")

# Copied verbatim from scripts/run_triggered_backtest_local.py -- keep
# these two dicts in sync by hand if that file's winning config ever
# changes; deliberately duplicated rather than imported, so a change made
# for a NEW local backtest experiment can't silently alter what's already
# running live in paper trading.
FILTER_CFG: dict = {
    "rsi_min": 60,
    "rsi_max": 100,
    "ema_fast": 50,
    "ema_slow": 200,
    "mom_lookback_days_short": 63,
    "mom_lookback_days_long": 126,
    "skip_recent_days": 5,
    "rsi_exit_gate_enabled": False,
    "weekly_monthly_gate_enabled": True,
    "advanced_equal_weight_sizing": False,
    "equal_weight_tolerance_pct": 0.20,
    "near_high_threshold": 0.85,
    "fundamental_gate_enabled": True,
    "fundamental_bonus_weight": 0.50,
    "min_fundamental_score": 50.0,
    "sector_bonus_weight": 1.00,
    "sector_diversification_enabled": False,
    "sector_composite_score_enabled": True,
    "history_days": 1200,
}

HA_CFG: dict = {
    "heikin_ashi_enabled": True,
    "ha_ema21_bounce_enabled": False,
    "multiyear_breakout_enabled": False,
    "shortterm_breakout_enabled": False,
    "pullback_slow_ema_enabled": False,
    "pullback_fast_ema_enabled": False,
    "watchlist_size": 20,
    "profit_target_rr": 2.0,
    "max_loss_pct_per_trade": 1.0,
    "ha_stop_mode": "atr",
    "ha_stop_atr_multiple": 1.0,
    "trailing_stop_enabled": True,
    "trailing_atr_multiple": 1.0,
    "ha_target_enabled": False,
    "ha_signal_lookback_days": 2,
    "monthly_trend_persistence_enabled": False,
    "sector_above_ema_enabled": True,
    "sector_overextension_enabled": False,
    "ha_breakeven_trail_enabled": False,
    "max_positions": 5,
}

PAPER_CFG: dict = {**ts.TRIGGERED_DEFAULTS, **FILTER_CFG, **HA_CFG}

DAYS_HISTORY = 1200  # matches FILTER_CFG["history_days"]


def load_market_data(end_date: dt.date) -> dict:
    """Fetches real (live, offline=False) Kite data through end_date, via
    the SAME read-only functions the backtest uses -- no new fetch path.
    Symbol universe = config.UNIVERSE plus whatever's currently held in
    the paper book (a symbol that fell out of the tracked universe would
    otherwise silently vanish from `candles`, and its stop would stop
    being checked)."""
    held = set(paper_db.get_open_positions().keys())
    symbols = sorted(set(config.UNIVERSE) | held)

    print(f"[paper_engine] fetching candles for {len(symbols)} symbols (live Kite)...")
    candles, bench = load_candles_cached(symbols, DAYS_HISTORY, offline=False)

    print("[paper_engine] fetching deep history for weekly/monthly gate...")
    long_candles = load_long_history_cached(symbols, end_date=end_date)

    print("[paper_engine] fetching sector membership + index candles (live Kite)...")
    sector_membership, sector_candles = su.sector_membership_and_candles(
        config.UNIVERSE, days=DAYS_HISTORY, verbose=False)

    fundamentals_history = None
    if os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
        fundamentals_history = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)["history"]
    else:
        print("[paper_engine][warn] no fundamentals_history.pkl -- fundamental gate "
             "fails OPEN (every score treated unknown), silently diverging from "
             "whatever the validated backtest actually had available.")

    print("[paper_engine] precomputing indicators...")
    precomputed = {}
    for sym, df in candles.items():
        if not df.empty and len(df) >= PAPER_CFG["ema_slow"]:
            precomputed[sym] = indicators.precompute_daily_series(df, PAPER_CFG)

    precomputed_weekly_monthly = {}
    if PAPER_CFG.get("weekly_monthly_gate_enabled", False):
        for sym, df in long_candles.items():
            if not df.empty:
                precomputed_weekly_monthly[sym] = indicators.precompute_weekly_monthly_bars(df["close"])

    precomputed_ha = {}
    for sym, df in candles.items():
        if not df.empty:
            precomputed_ha[sym] = ti.precompute_heikin_ashi(df)

    return {
        "candles": candles, "bench": bench, "long_candles": long_candles,
        "sector_candles": sector_candles, "sector_membership": sector_membership,
        "fundamentals_history": fundamentals_history,
        "precomputed": precomputed,
        "precomputed_weekly_monthly": precomputed_weekly_monthly,
        "precomputed_ha": precomputed_ha,
        "score_cache": {},
    }


def run_paper_day(date: pd.Timestamp, data: dict, dry_run: bool = False) -> dict:
    """Evaluates ONE trading day against the paper book. Ports
    backtest_triggered.py's day loop steps 1 (stop check, :213-224), 1b
    (trailing ratchet, generic-ATR branch only, :226-269), 2 (watchlist
    refresh, :317-345), 2b (trigger scan/fill, :347-409), 3 (mark to
    market, :411-415) -- 1c/1d are dead code under PAPER_CFG (no target,
    no institutional positions -- see module docstring) and are not
    ported. dry_run=True computes and returns everything but performs no
    paper_db writes."""
    cfg = PAPER_CFG
    candles, bench = data["candles"], data["bench"]
    sector_candles, sector_membership = data["sector_candles"], data["sector_membership"]
    long_candles = data["long_candles"]
    fundamentals_history = data["fundamentals_history"]
    precomputed = data["precomputed"]
    precomputed_weekly_monthly = data["precomputed_weekly_monthly"]
    precomputed_ha = data["precomputed_ha"]
    score_cache = data["score_cache"]

    cash = paper_db.get_cash()
    positions = paper_db.get_open_positions()
    actions: list[str] = []
    n_entries = n_exits = 0

    def _price_asof(sym: str) -> float | None:
        df = candles.get(sym)
        if df is None:
            return None
        sliced = df.loc[:date, "close"]
        return float(sliced.iloc[-1]) if not sliced.empty else None

    # 1) stop check
    for sym, pos in list(positions.items()):
        df = candles.get(sym)
        if df is None or date not in df.index:
            continue
        bar = df.loc[date]
        stop = float(pos["current_stop"])
        if float(bar["low"]) <= stop:
            fill = min(stop, float(bar["high"]))
            if float(bar["open"]) < stop:
                fill = min(fill, float(bar["open"]))
            initial_stop = pos.get("initial_stop")
            stop_type = ("initial" if initial_stop is not None
                        and abs(stop - float(initial_stop)) < 1e-6 else "trailing")
            actions.append(f"{date.date()} STOP-OUT {sym} @ {fill:.2f} ({stop_type})")
            if not dry_run:
                paper_db.close_position(sym, str(date.date()), fill, "stop", stop_type)
            cash += pos["qty"] * fill
            del positions[sym]
            n_exits += 1

    # 1b) trailing ratchet (generic ATR-chandelier branch only -- the only
    # branch reachable under PAPER_CFG: sector/institutional/breakeven
    # variants are all off)
    if cfg.get("trailing_stop_enabled", False):
        for sym, pos in positions.items():
            df = candles.get(sym)
            if df is None or date not in df.index:
                continue
            highest_close = max(float(pos["highest_close"]), float(df.loc[date, "close"]))
            atr_now = float(indicators.atr(df.loc[:date], cfg["atr_period"]).iloc[-1])
            new_stop = highest_close - cfg["trailing_atr_multiple"] * atr_now
            if new_stop > float(pos["current_stop"]):
                if not dry_run:
                    paper_db.ratchet_stop(sym, str(date.date()), new_stop, highest_close, atr_now)
                pos["current_stop"] = new_stop
                pos["highest_close"] = highest_close

    # 2) watchlist refresh (rebalance="D" in the validated backtest -- every day)
    ranked = rank_universe_asof(candles, bench, date, cfg, fundamentals_history,
                                score_cache, sector_candles, sector_membership,
                                long_candles, precomputed, None, precomputed_weekly_monthly)
    watchlist: dict[str, pd.Series] = {}
    if not ranked.empty:
        candidates = ranked[ranked["all_gates"]]
        watch_syms = [s for s in candidates.index if s not in positions]
        watch_syms = _apply_sector_cap(watch_syms, positions, ranked, cfg)
        watchlist = {sym: candidates.loc[sym] for sym in watch_syms[:cfg["watchlist_size"]]}

    watchlist_report = []

    # 2b) trigger scan + fill
    if watchlist and len(positions) < cfg["max_positions"]:
        ordered = sorted(watchlist.items(), key=lambda kv: kv[1].get("score", 0), reverse=True)
        for rank, (sym, row) in enumerate(ordered, start=1):
            entry = {"rank": rank, "symbol": sym, "score": round(float(row.get("score", 0)), 3),
                     "sector": row.get("top_sector")}
            if len(positions) >= cfg["max_positions"]:
                entry["result"] = "skipped (max_positions full)"
                watchlist_report.append(entry)
                continue
            if sym not in candles or date not in candles[sym].index:
                entry["result"] = "no data today"
                watchlist_report.append(entry)
                continue

            df_upto = candles[sym].loc[:date]
            sector_df_upto = None
            sym_sector = row.get("top_sector") if hasattr(row, "get") else None
            sector_df = sector_candles.get(sym_sector) if sym_sector else None
            if sector_df is not None and not sector_df.empty:
                sector_df_upto = sector_df.loc[:date]
            ha_upto = precomputed_ha.get(sym)
            ha_upto = ha_upto.loc[:date] if ha_upto is not None else None

            trig = ts.detect_trigger(df_upto, cfg, sector_df_upto, ha_upto)
            if trig is None:
                entry["result"] = "no trigger"
                watchlist_report.append(entry)
                continue

            price = trig["price"]
            initial_stop = trig["stop"]
            equity_now = cash + sum(p["qty"] * (_price_asof(s) or 0.0) for s, p in positions.items())
            qty = ts.trigger_position_size(equity_now, price, initial_stop,
                                           cfg["max_positions"], cfg["max_loss_pct_per_trade"])
            qty = min(qty, int(cash / price)) if price > 0 else 0
            if qty <= 0:
                entry["result"] = "triggered but qty=0 (insufficient cash/risk cap)"
                watchlist_report.append(entry)
                continue

            snapshot = {
                "score": float(row.get("score", 0)), "rsi": row.get("rsi"),
                "pct_52w_high": row.get("pct_52w_high"), "vol_expansion": row.get("vol_expansion"),
                "fundamental_score": row.get("fundamental_score"),
                "entry_reason": f"rank #{rank}/{len(watchlist)} (score {row.get('score', 0):.2f}); "
                               f"sector {sym_sector or 'n/a'}",
            }
            actions.append(f"{date.date()} BUY {sym} x{qty} @ {price:.2f} stop {initial_stop:.2f} "
                          f"trigger={trig['type']}")
            if not dry_run:
                paper_db.open_position(sym, str(date.date()), price, qty, initial_stop,
                                       snapshot, trig["type"])
            cash -= qty * price
            positions[sym] = {"symbol": sym, "qty": qty, "entry_price": price,
                              "current_stop": initial_stop, "initial_stop": initial_stop,
                              "highest_close": price}
            n_entries += 1
            entry["result"] = f"ENTERED x{qty} @ {price:.2f}"
            watchlist_report.append(entry)

    # 3) mark to market
    equity = cash + sum(p["qty"] * (_price_asof(s) or 0.0) for s, p in positions.items())
    if not dry_run:
        paper_db.set_cash(cash)
        paper_db.log_equity(str(date.date()), equity)

    return {
        "date": date, "cash": cash, "equity": equity,
        "n_watchlist": len(watchlist), "n_entries": n_entries, "n_exits": n_exits,
        "watchlist_report": watchlist_report, "actions": actions,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print, write nothing to cache/state_paper.db")
    ap.add_argument("--date", type=str, default=None,
                    help="force a single specific date (YYYY-MM-DD) instead of catch-up")
    ap.add_argument("--reset", action="store_true",
                    help="delete cache/state_paper.db and exit (does not run a scan)")
    args = ap.parse_args(argv)

    if args.reset:
        paper_db.reset()
        print("[paper_engine] paper DB reset (cache/state_paper.db deleted).")
        return

    paper_db.ensure_initialised()

    today = dt.date.today()
    if args.date:
        todo = [dt.date.fromisoformat(args.date)]
    else:
        last = paper_db.last_scan_date()
        start = (dt.date.fromisoformat(last) + dt.timedelta(days=1)) if last else today
        todo = []
        d = start
        while d <= today:
            if nse_holidays.is_trading_day(d):
                todo.append(d)
            d += dt.timedelta(days=1)

    if not todo:
        print("[paper_engine] nothing due -- already caught up.")
        return

    print(f"[paper_engine] {len(todo)} trading day(s) to evaluate: "
         f"{todo[0].isoformat()} to {todo[-1].isoformat()}"
         f"{' (dry-run, no writes)' if args.dry_run else ''}")

    data = load_market_data(todo[-1])

    for d in todo:
        ts_date = pd.Timestamp(d)
        if ts_date not in data["bench"].index:
            print(f"[paper_engine] {d.isoformat()}: no bench data (holiday/gap) -- skipping")
            if not args.dry_run:
                paper_db.record_scan(d.isoformat(), "skipped_no_data")
            continue
        try:
            result = run_paper_day(ts_date, data, dry_run=args.dry_run)
        except Exception as e:
            print(f"[paper_engine] {d.isoformat()}: ERROR -- {e}")
            if not args.dry_run:
                paper_db.record_scan(d.isoformat(), "error", error_message=str(e))
            raise
        for line in result["actions"]:
            print(f"  {line}")
        print(f"[paper_engine] {d.isoformat()}: watchlist={result['n_watchlist']} "
             f"entries={result['n_entries']} exits={result['n_exits']} "
             f"cash={result['cash']:,.0f} equity={result['equity']:,.0f}")
        if not args.dry_run:
            import json
            paper_db.record_scan(d.isoformat(), "ok", n_watchlist=result["n_watchlist"],
                                 n_entries=result["n_entries"], n_exits=result["n_exits"],
                                 cash=result["cash"], equity=result["equity"],
                                 watchlist_json=json.dumps(result["watchlist_report"]))

    print("[paper_engine] done.")


if __name__ == "__main__":
    main()
