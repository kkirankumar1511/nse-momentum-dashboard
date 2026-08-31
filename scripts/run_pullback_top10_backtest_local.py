"""
Two-stage selection test, explicit request: "get the top 20 watchlist
first and on top of that.. score separately for EMA pullback proximity
and take top 10 based on EMA pullback proximity score.. and see the
result." Distinct from config.STRATEGY["ema_pullback_weight"] (which
BLENDS the pullback factor into the one core score) -- this instead:

  1. Ranks the full universe with the UNCHANGED core score
     (ema_pullback_weight=0) -- the exact live scoring, no pullback
     influence at all -- and takes the top 20 gate-passers as today's
     watchlist, same as every other script this session.
  2. Among just those 20, computes ema_pullback_proximity separately
     (indicators.precompute_ema_pullback_proximity) and re-sorts by
     THAT alone -- a stock not currently in a pullback (NaN proximity)
     sorts last.
  3. Only the top 10 of that pullback-sorted list are eligible for a
     NEW buy each day. A stock ranked #2 by core score but not
     currently pulled back could rank #15 of 20 here and miss out
     entirely, while a #18-by-core-score stock sitting right at EMA21
     jumps to the front.

Exits reuse the exact production rule (screener.sell_check: dropped
below 200 EMA, or fell out of the ORIGINAL top-20 core-score keep zone
-- proximity only decides which of the 20 gets BOUGHT, not when a held
position gets sold) plus the standard ATR stop + trailing stop (same
formula/defaults as backtest.py's own production engine).

Run with: python scripts/run_pullback_top10_backtest_local.py --days 756 --max-positions 10
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import backtest as bt
import config
import indicators
import screener

FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")
SECTOR_DATA_CACHE = os.path.join("cache", "sector_data.pkl")

EMA_BLEND_NEAR_PCT = 0.02  # 2% -- only blend the initial ATR stop toward EMA13/21
                          # when the ATR stop already lands near EMA13 (see
                          # run_ema_stop_backtest_local.py's blended_stop, same rule)


def blended_stop(entry_price, atr_now, atr_stop_multiple, ema13_v, ema21_v):
    atr_stop = entry_price - atr_stop_multiple * atr_now
    if ema13_v is None or pd.isna(ema13_v) or ema21_v is None or pd.isna(ema21_v) or ema13_v == 0:
        return atr_stop
    if abs(atr_stop - ema13_v) / ema13_v <= EMA_BLEND_NEAR_PCT:
        if atr_stop > ema13_v:
            return min(ema13_v, atr_stop)
        else:
            return min(ema21_v, atr_stop)
    return atr_stop


def run_backtest(scan_dates, long_candles, precomputed, precomputed_ema_pullback,
                 bench, fundamentals_history, sector_candles, sector_membership,
                 watchlist_size, max_positions, atr_stop_multiple, trailing_atr_multiple,
                 initial_capital=1_000_000, entry_rsi_min=None, sector_bonus_weight=None,
                 max_new_per_day=None, rank_exit_enabled=True, target_rr=None,
                 entry_pool_size=None, cfg_overrides=None, precomputed_weekly_monthly_ok=None,
                 ema_stop_blend_enabled=False, precomputed_ema13=None, precomputed_ema21=None,
                 confirm_days=None, confirm_pool_size=None):
    cash = initial_capital
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    equity_curve: list[tuple] = []
    candidate_streak: dict[str, int] = {}
    cfg = dict(config.STRATEGY)
    cfg["ema_pullback_weight"] = 0.0  # core ranking stays UNCHANGED, per design
    if cfg_overrides:
        cfg.update(cfg_overrides)
    if sector_bonus_weight is not None:
        cfg["sector_bonus_weight"] = sector_bonus_weight

    bench_above_regime_ema = None
    if cfg.get("regime_filter_enabled", False):
        regime_ema = indicators.ema(bench["close"], cfg.get("regime_ema_period", 200))
        bench_above_regime_ema = bench["close"] > regime_ema

    for date in scan_dates:
        effective_max_positions = max_positions
        if bench_above_regime_ema is not None:
            regime_ok = bool(bench_above_regime_ema.get(date, True))
            effective_max_positions = max_positions if regime_ok else \
                max(1, int(max_positions * cfg.get("regime_position_multiplier", 0.5)))
        # 1) stop checks (gap-aware, same fill rule as backtest.py's own engine)
        for sym in list(positions.keys()):
            df = long_candles.get(sym)
            if df is None or date not in df.index:
                continue
            row = df.loc[date]
            pos = positions[sym]
            exit_price = None
            reason = None
            if row["low"] <= pos["stop"]:
                exit_price = min(pos["stop"], row["high"])
                exit_price = min(exit_price, row["open"]) if row["open"] < pos["stop"] else exit_price
                reason = "stop"
            elif pos.get("target") is not None and row["high"] >= pos["target"]:
                exit_price = pos["target"]
                reason = "target"
            if exit_price is not None:
                pnl = (exit_price - pos["entry_price"]) * pos["qty"]
                ret_pct = (exit_price / pos["entry_price"] - 1) * 100
                cash += pos["qty"] * exit_price
                trades.append({
                    "symbol": sym, "entry_date": pos["entry_date"], "exit_date": date,
                    "entry_price": pos["entry_price"], "exit_price": exit_price,
                    "qty": pos["qty"], "reason": reason, "pnl": pnl, "ret_pct": ret_pct,
                    "holding_days": (date - pos["entry_date"]).days,
                })
                del positions[sym]

        # 1b) trailing stop ratchet
        for sym, pos in positions.items():
            df = long_candles.get(sym)
            pdf = precomputed.get(sym)
            if df is None or pdf is None or date not in df.index or date not in pdf.index:
                continue
            pos["highest_close"] = max(pos["highest_close"], float(df.loc[date, "close"]))
            atr_now = float(pdf.loc[date, "atr"])
            new_stop = pos["highest_close"] - trailing_atr_multiple * atr_now
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop

        # 2) rank with UNCHANGED core score, top-20 watchlist
        ranked = bt.rank_universe_asof(
            long_candles, bench, date, cfg,
            fundamentals_history, {}, sector_candles, sector_membership,
            long_candles, precomputed, None, None, precomputed_weekly_monthly_ok)
        if ranked.empty:
            equity_curve.append((date, cash + sum(
                p["qty"] * float(long_candles[s].loc[date, "close"])
                for s, p in positions.items() if date in long_candles[s].index)))
            continue
        candidates = ranked[ranked["all_gates"]].sort_values("score", ascending=False)
        top20 = candidates.head(watchlist_size)
        keep_zone = set(top20.index)

        # entry-confirmation streak (mirrors run_ema_stop_backtest_local.py's
        # candidate_streak): consecutive days a symbol has stayed in the
        # confirm-pool (defaults to the top-20 watchlist; --confirm-pool-size
        # narrows it e.g. to top-10). Reset to 0 the day it drops out.
        confirm_pool = candidates.head(confirm_pool_size) if confirm_pool_size is not None else top20
        confirm_syms_today = set(confirm_pool.index)
        for sym in list(candidate_streak.keys()):
            if sym not in confirm_syms_today:
                candidate_streak[sym] = 0
        for sym in confirm_syms_today:
            candidate_streak[sym] = candidate_streak.get(sym, 0) + 1

        # 2b) sells: same production rule (200 EMA / fell out of top-20 keep zone)
        # -- explicit request ("The ATR stop/trail" [only]): rank_exit_enabled=False
        # skips this block entirely, so a position only ever exits via the
        # stop/trailing-stop check in step 1 above, never on rank/gate churn.
        if rank_exit_enabled:
            for sym in list(positions.keys()):
                reason = screener.sell_check(sym, ranked, candidates, keep_zone, watchlist_size // 2, cfg)
                if reason:
                    df = long_candles.get(sym)
                    if df is None or date not in df.index:
                        continue
                    exit_price = float(df.loc[date, "close"])
                    pos = positions[sym]
                    pnl = (exit_price - pos["entry_price"]) * pos["qty"]
                    ret_pct = (exit_price / pos["entry_price"] - 1) * 100
                    cash += pos["qty"] * exit_price
                    trades.append({
                        "symbol": sym, "entry_date": pos["entry_date"], "exit_date": date,
                        "entry_price": pos["entry_price"], "exit_price": exit_price,
                        "qty": pos["qty"], "reason": reason, "pnl": pnl, "ret_pct": ret_pct,
                        "holding_days": (date - pos["entry_date"]).days,
                    })
                    del positions[sym]

        # 3) among the top 20, re-sort by pullback proximity alone; top
        # max_new_per_day (explicit request "keep only 1") eligible for a
        # NEW buy today -- defaults to max_positions (previous behavior)
        # when not set.
        # 2026-08-28 fix, explicit request (real trade trace: GVT&D
        # 2026-04-27/06-19 both bought with NaN proximity -- close was
        # 11-12% ABOVE EMA21, not a pullback at all): a stock with NaN
        # proximity (doesn't qualify as a pullback -- see indicators.
        # precompute_ema_pullback_proximity, which is NaN whenever
        # close > EMA21) is now EXCLUDED entirely, not just sorted last.
        # Previously NaN -> -inf still let it backfill an open slot on
        # days when fewer than max_positions stocks in the top 20 were
        # genuinely pulled back -- silently diluting the whole "pullback"
        # thesis with ordinary momentum picks. Now such a slot is simply
        # left unfilled that day instead.
        # explicit request ("subrank from first top 10 based on pullback
        # rank... rest logic based on ATR and max position*2 as is"): the
        # pullback re-rank/entry pool is restricted to entry_pool_size
        # (10) -- the BEST HALF of the top-20 watchlist by core score --
        # while keep_zone (sells) above still uses the full watchlist_size
        # (20), unchanged from production.
        entry_pool = top20.head(entry_pool_size) if entry_pool_size is not None else top20
        proximity = {}
        for sym in entry_pool.index:
            pser = precomputed_ema_pullback.get(sym)
            val = pser.loc[date] if pser is not None and date in pser.index else None
            if val is not None and not pd.isna(val):
                proximity[sym] = float(val)
        by_proximity = sorted(proximity.keys(), key=lambda s: -proximity[s])
        # explicit request ("dip analysis... factor for loss" -> RSI floor
        # test): if entry_rsi_min is set, a candidate failing it is
        # skipped BEFORE the top-N cut, so the next-best-proximity name
        # still eligible takes its slot instead of leaving it unfilled.
        if entry_rsi_min is not None:
            def _passes_rsi(sym):
                pdf = precomputed.get(sym)
                if pdf is None or date not in pdf.index:
                    return False
                return float(pdf.loc[date, "rsi"]) >= entry_rsi_min
            by_proximity = [s for s in by_proximity if _passes_rsi(s)]
        if confirm_days is not None:
            by_proximity = [s for s in by_proximity if candidate_streak.get(s, 0) >= confirm_days]
        top10_by_proximity = by_proximity[: (max_new_per_day if max_new_per_day is not None else max_positions)]

        for sym in top10_by_proximity:
            if sym in positions or len(positions) >= effective_max_positions:
                continue
            df = long_candles.get(sym)
            pdf = precomputed.get(sym)
            if df is None or pdf is None or date not in df.index or date not in pdf.index:
                continue
            equity_now = cash + sum(
                p["qty"] * float(long_candles[s].loc[date, "close"])
                for s, p in positions.items() if date in long_candles[s].index)
            slot_capital = equity_now / max_positions
            entry_price = float(df.loc[date, "close"])
            qty = int(slot_capital / entry_price)
            if qty <= 0 or qty * entry_price > cash:
                continue
            atr_now = float(pdf.loc[date, "atr"])
            if ema_stop_blend_enabled:
                ema13_ser = precomputed_ema13.get(sym) if precomputed_ema13 else None
                ema21_ser = precomputed_ema21.get(sym) if precomputed_ema21 else None
                ema13_v = ema13_ser.loc[date] if ema13_ser is not None and date in ema13_ser.index else None
                ema21_v = ema21_ser.loc[date] if ema21_ser is not None and date in ema21_ser.index else None
                initial_stop = blended_stop(entry_price, atr_now, atr_stop_multiple, ema13_v, ema21_v)
            else:
                initial_stop = entry_price - atr_stop_multiple * atr_now
            cash -= qty * entry_price
            positions[sym] = {
                "entry_price": entry_price, "stop": initial_stop,
                "target": entry_price + target_rr * (entry_price - initial_stop)
                         if target_rr is not None else None,
                "highest_close": entry_price, "qty": qty, "entry_date": date,
            }

        equity_now = cash + sum(
            p["qty"] * float(long_candles[s].loc[date, "close"])
            for s, p in positions.items() if date in long_candles[s].index)
        equity_curve.append((date, equity_now))

    return (pd.DataFrame(trades),
           pd.DataFrame(equity_curve, columns=["date", "equity"]).set_index("date"),
           len(positions))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--watchlist-size", type=int, default=20)
    ap.add_argument("--max-positions", type=int, default=10)
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--atr-stop-multiple", type=float, default=2.5)
    ap.add_argument("--trailing-atr-multiple", type=float, default=4.0)
    ap.add_argument("--entry-rsi-min", type=float, default=None,
                    help="skip a pullback candidate if its entry-day RSI is below this "
                        "(explicit test after loss-factor analysis showed the RSI 50-60 "
                        "band was the weakest)")
    ap.add_argument("--sector-bonus-weight", type=float, default=None,
                    help="overrides sector_bonus_weight (default 0.0) for the top-20 "
                        "core-score ranking stage")
    ap.add_argument("--max-new-per-day", type=int, default=None,
                    help="cap on new pullback-ranked entries taken per day (default: "
                        "same as --max-positions); explicit request 'keep only 1'")
    ap.add_argument("--no-rank-exit", action="store_true",
                    help="disables the rank/gate-based sell rule entirely -- a position "
                        "then only ever exits via the ATR stop/trailing-stop check")
    ap.add_argument("--target-rr", type=float, default=None,
                    help="adds a fixed target = entry + this * (entry - initial_stop), "
                        "frozen at entry -- e.g. 1.0 for a 1:1 risk/reward target")
    ap.add_argument("--entry-pool-size", type=int, default=None,
                    help="restricts the pullback re-rank/entry pool to just the top N "
                        "of the watchlist by core score (e.g. 10 = best half of a "
                        "20-name watchlist) -- sells/keep-zone still use the full "
                        "--watchlist-size, unchanged")
    ap.add_argument("--pullback-fast-ema", type=int, default=None,
                    help="overrides ema_pullback_fast (default 13) for the proximity calc")
    ap.add_argument("--pullback-slow-ema", type=int, default=None,
                    help="overrides ema_pullback_mid (default 21) for the proximity calc "
                        "-- the 'must close <= this EMA' line")
    ap.add_argument("--start-date", type=str, default=None,
                    help="YYYY-MM-DD -- scan from this date instead of trailing --days")
    ap.add_argument("--rsi-min", type=float, default=None)
    ap.add_argument("--rsi-max", type=float, default=None)
    ap.add_argument("--core-ema-fast", type=int, default=None,
                    help="overrides cfg['ema_fast'] -- the CORE trend-gate fast EMA "
                        "(default 50), distinct from --pullback-fast-ema")
    ap.add_argument("--core-ema-slow", type=int, default=None,
                    help="overrides cfg['ema_slow'] -- the CORE trend-gate slow EMA "
                        "(default 200), distinct from --pullback-slow-ema")
    ap.add_argument("--mom-lookback-short", type=int, default=None,
                    help="overrides mom_lookback_days_short (default 63)")
    ap.add_argument("--mom-lookback-long", type=int, default=None,
                    help="overrides mom_lookback_days_long (default 126)")
    ap.add_argument("--rsi-exit-gate", action="store_true",
                    help="overrides rsi_exit_gate_enabled -> True, rsi_exit_max -> 100.0")
    ap.add_argument("--weekly-monthly-gate", action="store_true",
                    help="overrides weekly_monthly_gate_enabled -> True")
    ap.add_argument("--mom-method", choices=["fixed_lookback", "regression"], default=None,
                    help="overrides mom_method (default fixed_lookback)")
    ap.add_argument("--regime-filter", action="store_true",
                    help="overrides regime_filter_enabled -> True -- halves max_positions "
                        "on any day NIFTY closes below its own regime_ema_period (200) EMA")
    ap.add_argument("--confirm-days", type=int, default=None,
                    help="require a stock to have stayed in the confirm-pool for this "
                        "many consecutive days before a NEW buy is allowed, e.g. 2")
    ap.add_argument("--confirm-pool-size", type=int, default=None,
                    help="the pool the confirm-days streak is measured against -- "
                        "defaults to --watchlist-size (top-20); pass 10 for top-10")
    ap.add_argument("--ema-stop-blend", action="store_true",
                    help="enable the EMA13/21-blended initial stop (default off)")
    ap.add_argument("--out-suffix", type=str, default="",
                    help="appends to output filenames so multiple runs don't clobber "
                        "each other (default: fixed pullback_top10_trades/equity.csv)")
    args = ap.parse_args()

    fundamentals_history = None
    if os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
        fundamentals_history = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)["history"]
        print(f"Loaded fundamentals_history ({len(fundamentals_history)} symbols).")

    sector_membership = sector_candles = None
    if os.path.exists(SECTOR_DATA_CACHE):
        _sd = pd.read_pickle(SECTOR_DATA_CACHE)
        sector_membership, sector_candles = _sd["sector_membership"], _sd["sector_candles"]
        print(f"Loaded sector data cached {_sd['run_time']}.")

    end_date = dt.date.today()
    long_candles = bt.load_long_history_cached(config.UNIVERSE, end_date=end_date)
    print(f"Loaded long_candles for {len(long_candles)} symbols.")
    bench = bt._tz_naive(pd.read_csv(os.path.join(bt.LONG_CACHE_DIR, "_NIFTY.csv"),
                                     index_col=0, parse_dates=True))

    all_dates = bench.index.sort_values()
    if args.start_date is not None:
        scan_dates = all_dates[all_dates >= pd.Timestamp(args.start_date)]
        print(f"Overriding start date -> {args.start_date} (from --start-date).")
    else:
        scan_dates = all_dates[-args.days:]
    print(f"\nScanning {len(scan_dates)} trading days: "
         f"{scan_dates[0].date()} to {scan_dates[-1].date()}\n")

    core_cfg_overrides = {}
    if args.rsi_min is not None:
        core_cfg_overrides["rsi_min"] = args.rsi_min
    if args.rsi_max is not None:
        core_cfg_overrides["rsi_max"] = args.rsi_max
    if args.core_ema_fast is not None:
        core_cfg_overrides["ema_fast"] = args.core_ema_fast
    if args.core_ema_slow is not None:
        core_cfg_overrides["ema_slow"] = args.core_ema_slow
    if args.mom_lookback_short is not None:
        core_cfg_overrides["mom_lookback_days_short"] = args.mom_lookback_short
    if args.mom_lookback_long is not None:
        core_cfg_overrides["mom_lookback_days_long"] = args.mom_lookback_long
    if args.rsi_exit_gate:
        core_cfg_overrides["rsi_exit_gate_enabled"] = True
        core_cfg_overrides["rsi_exit_max"] = 100.0
    if args.weekly_monthly_gate:
        core_cfg_overrides["weekly_monthly_gate_enabled"] = True
    if args.mom_method is not None:
        core_cfg_overrides["mom_method"] = args.mom_method
    if args.regime_filter:
        core_cfg_overrides["regime_filter_enabled"] = True
    if core_cfg_overrides:
        print(f"Overriding core cfg: {core_cfg_overrides}")

    cfg = dict(config.STRATEGY)
    cfg.update(core_cfg_overrides)
    if args.pullback_fast_ema is not None:
        cfg["ema_pullback_fast"] = args.pullback_fast_ema
        print(f"Overriding ema_pullback_fast -> {args.pullback_fast_ema} (from --pullback-fast-ema).")
    if args.pullback_slow_ema is not None:
        cfg["ema_pullback_mid"] = args.pullback_slow_ema
        print(f"Overriding ema_pullback_mid -> {args.pullback_slow_ema} (from --pullback-slow-ema).")
    print("Precomputing daily indicators per symbol...")
    precomputed = {}
    for sym, df in long_candles.items():
        if not df.empty and len(df) >= cfg["ema_slow"]:
            precomputed[sym] = indicators.precompute_daily_series(df, cfg)

    # Speed-up for the weekly/monthly confirmation gate (see
    # indicators.precompute_weekly_monthly_trend_ok's docstring) -- a
    # plain daily-indexed True/False/NA lookup instead of re-resampling
    # each symbol's full history into weekly/monthly bars on every
    # single scan day, profiled elsewhere in this codebase at ~45% of a
    # gate-enabled backtest's total runtime. Only built when the gate is
    # actually on, since it's wasted work otherwise.
    precomputed_weekly_monthly_ok = None
    if cfg.get("weekly_monthly_gate_enabled", False):
        print("Precomputing weekly/monthly trend-ok per symbol (gate is ON)...")
        precomputed_weekly_monthly_ok = {}
        for sym, df in long_candles.items():
            if not df.empty:
                precomputed_weekly_monthly_ok[sym] = indicators.precompute_weekly_monthly_trend_ok(df, cfg)

    print("Precomputing EMA pullback proximity per symbol...")
    precomputed_ema_pullback = {}
    for sym, df in long_candles.items():
        if not df.empty:
            precomputed_ema_pullback[sym] = indicators.precompute_ema_pullback_proximity(df, cfg)

    precomputed_ema13 = precomputed_ema21 = None
    if args.ema_stop_blend:
        print("Precomputing EMA13/EMA21 per symbol (for stop blending)...")
        precomputed_ema13, precomputed_ema21 = {}, {}
        for sym, df in long_candles.items():
            if not df.empty:
                precomputed_ema13[sym] = indicators.ema(df["close"], 13)
                precomputed_ema21[sym] = indicators.ema(df["close"], 21)

    trades, equity_curve, open_at_end = run_backtest(
        scan_dates, long_candles, precomputed, precomputed_ema_pullback,
        bench, fundamentals_history, sector_candles, sector_membership,
        args.watchlist_size, args.max_positions, args.atr_stop_multiple,
        args.trailing_atr_multiple, initial_capital=args.capital,
        entry_rsi_min=args.entry_rsi_min, sector_bonus_weight=args.sector_bonus_weight,
        max_new_per_day=args.max_new_per_day, rank_exit_enabled=not args.no_rank_exit,
        target_rr=args.target_rr, entry_pool_size=args.entry_pool_size,
        cfg_overrides=core_cfg_overrides, precomputed_weekly_monthly_ok=precomputed_weekly_monthly_ok,
        ema_stop_blend_enabled=args.ema_stop_blend, precomputed_ema13=precomputed_ema13,
        precomputed_ema21=precomputed_ema21, confirm_days=args.confirm_days,
        confirm_pool_size=args.confirm_pool_size)

    print(f"\n=== Top-{args.watchlist_size} watchlist, top-{args.max_positions} by "
         f"EMA pullback proximity, max {args.max_positions} concurrent positions "
         f"(regime_filter={args.regime_filter}, confirm_days={args.confirm_days}, "
         f"confirm_pool_size={args.confirm_pool_size}, ema_stop_blend={args.ema_stop_blend}) ===")
    print(f"Closed trades: {len(trades)}, open at end: {open_at_end}")
    if not trades.empty:
        wins = trades[trades["ret_pct"] > 0]
        win_rate = len(wins) / len(trades) * 100
        avg_ret = trades["ret_pct"].mean()
        gross_win = wins["pnl"].sum()
        gross_loss = -trades[trades["ret_pct"] <= 0]["pnl"].sum()
        profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
        print(f"Win rate: {win_rate:.1f}%")
        print(f"Avg return/trade: {avg_ret:.2f}%")
        print(f"Profit factor: {profit_factor:.2f}")
    if not equity_curve.empty:
        years = (equity_curve.index[-1] - equity_curve.index[0]).days / 365.25
        final_equity = equity_curve["equity"].iloc[-1]
        cagr = (final_equity / args.capital) ** (1 / years) - 1 if years > 0 else 0
        print(f"CAGR (annualized from {years:.2f}y window): {cagr*100:.2f}%")
        print(f"Final equity: {final_equity:,.2f} (started {args.capital:,.0f})")
    tr_path = f"result/pullback_top10_trades{args.out_suffix}.csv"
    eq_path = f"result/pullback_top10_equity{args.out_suffix}.csv"
    trades.to_csv(tr_path, index=False)
    equity_curve.to_csv(eq_path)
    print(f"Saved: {tr_path}, {eq_path}")


if __name__ == "__main__":
    main()
