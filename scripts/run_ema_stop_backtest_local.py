"""
Test: instead of a plain ATR initial stop, blend it toward EMA13/EMA21
when the ATR stop lands close to EMA13. Explicit request, after tracing
the LAURUSLABS 2026-07-03 stop-out (ATR stop landed -0.93% below EMA13,
+1.13% above EMA21):

  atr_stop = entry_price - atr_stop_multiple * ATR14   (unchanged baseline)
  if |atr_stop - EMA13| / EMA13 <= 2%:        (only when ATR stop is
                                                already near EMA13)
      if atr_stop > EMA13: final_stop = min(EMA13, atr_stop)  (= EMA13,
                            widens the stop down to EMA13 support)
      else:                final_stop = min(EMA21, atr_stop)  (widens
                            further to EMA21 only if EMA21 is lower
                            still than the already-sub-EMA13 ATR stop)
  else:
      final_stop = atr_stop   (unchanged -- ATR stop isn't near EMA13,
                                no EMA blending)

Everything else (ranking, entries, rank-exit, trailing stop) is the
UNCHANGED production engine -- same top-20/take-top-10-by-score entries
as backtest.py's own run_backtest, just this one stop-placement tweak,
so results are directly comparable to run_production_backtest_local.py.

Run with: python scripts/run_ema_stop_backtest_local.py --start-date 2021-01-01 ...
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
from mad_trail_strategy import MAD_TRAIL_DEFAULTS, precompute_mad_trail

FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")
SECTOR_DATA_CACHE = os.path.join("cache", "sector_data.pkl")

EMA_BLEND_NEAR_PCT = 0.02  # 2% -- only blend when ATR stop is already near EMA13


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


def run_backtest(scan_dates, long_candles, precomputed, precomputed_ema13, precomputed_ema21,
                 bench, fundamentals_history, sector_candles, sector_membership,
                 watchlist_size, max_positions, atr_stop_multiple, trailing_atr_multiple,
                 initial_capital=1_000_000, cfg_overrides=None, precomputed_weekly_monthly=None,
                 precomputed_weekly_monthly_ok=None,
                 ema_stop_blend_enabled=False, target_rr=None, time_stop_days=None,
                 confirm_days=None, mom_cap=None, confirm_pool_size=None,
                 mad_stop_enabled=False, precomputed_mad=None):
    cash = initial_capital
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    equity_curve: list[tuple] = []
    candidate_streak: dict[str, int] = {}
    cfg = dict(config.STRATEGY)
    if cfg_overrides:
        cfg.update(cfg_overrides)

    # Market regime filter (mirrors backtest.py's own implementation exactly,
    # config.py:307-317 / backtest.py:788-935): caps -- never sizes -- how
    # many NEW positions may be open at once when NIFTY 50 itself is below
    # its own regime_ema_period EMA. Cap-only, so existing slot sizing is
    # untouched; just fewer total slots get filled during a weak market.
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

        # 1) stop / target / time-stop checks (gap-aware fill, same rule as
        # backtest.py's own engine). Explicit request ("if the price is not
        # reach 1:1 target within 20 days then exit at day 20"): a position
        # with a target that hasn't been hit within time_stop_days trading
        # days is force-closed at that day's close -- checked AFTER stop/
        # target so a same-day stop or target hit always takes priority.
        for sym in list(positions.keys()):
            df = long_candles.get(sym)
            if df is None or date not in df.index:
                continue
            row = df.loc[date]
            pos = positions[sym]
            pos["bars_held"] = pos.get("bars_held", 0) + 1
            exit_price = None
            reason = None
            if row["low"] <= pos["stop"]:
                exit_price = min(pos["stop"], row["high"])
                exit_price = min(exit_price, row["open"]) if row["open"] < pos["stop"] else exit_price
                reason = "stop"
            elif pos.get("target") is not None and row["high"] >= pos["target"]:
                exit_price = pos["target"]
                reason = "target"
            elif (time_stop_days is not None and pos.get("target") is not None
                 and pos["bars_held"] >= time_stop_days):
                exit_price = float(row["close"])
                reason = f"time_stop_{time_stop_days}d"
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

        # 1b) trailing stop ratchet -- MAD-trail mode (explicit request:
        # "keep entry logic as is.. only use the Trail as support (bull)")
        # replaces the ATR trailing mechanism entirely with the MAD trail's
        # own one-sided ratcheting lower band, read fresh each day; plain
        # ATR trail otherwise, unchanged.
        for sym, pos in positions.items():
            df = long_candles.get(sym)
            pdf = precomputed.get(sym)
            if df is None or pdf is None or date not in df.index or date not in pdf.index:
                continue
            pos["highest_close"] = max(pos["highest_close"], float(df.loc[date, "close"]))
            if mad_stop_enabled:
                mad = precomputed_mad.get(sym) if precomputed_mad else None
                if mad is not None and date in mad.index:
                    new_stop = float(mad.loc[date, "lower"])
                    if new_stop > pos["stop"]:
                        pos["stop"] = new_stop
                continue
            atr_now = float(pdf.loc[date, "atr"])
            new_stop = pos["highest_close"] - trailing_atr_multiple * atr_now
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop

        # 2) rank (unchanged production core score)
        ranked = bt.rank_universe_asof(
            long_candles, bench, date, cfg,
            fundamentals_history, {}, sector_candles, sector_membership,
            long_candles, precomputed, None, precomputed_weekly_monthly,
            precomputed_weekly_monthly_ok)
        if ranked.empty:
            equity_curve.append((date, cash + sum(
                p["qty"] * float(long_candles[s].loc[date, "close"])
                for s, p in positions.items() if date in long_candles[s].index)))
            continue
        candidates = ranked[ranked["all_gates"]].sort_values("score", ascending=False)
        top20 = candidates.head(watchlist_size)
        keep_zone = set(top20.index)

        # explicit request ("entry confirmation filter... require a stock
        # to have stayed in the top-20 for 2 consecutive days before
        # buying", then "test with top 10 consecutive days as well"):
        # track a running consecutive-day-in-pool streak per symbol,
        # reset to 0 the moment it drops out. confirm_pool_size lets the
        # POOL used for this streak be narrower than the watchlist itself
        # (e.g. top-10 by score) -- defaults to watchlist_size (top-20)
        # when not set.
        confirm_pool = candidates.head(confirm_pool_size) if confirm_pool_size is not None else top20
        confirm_syms_today = set(confirm_pool.index)
        for sym in list(candidate_streak.keys()):
            if sym not in confirm_syms_today:
                candidate_streak[sym] = 0
        for sym in confirm_syms_today:
            candidate_streak[sym] = candidate_streak.get(sym, 0) + 1

        # 2b) sells: unchanged production rank/gate exit rule
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

        # 3) entries: unchanged production rule -- top of the top-20 by
        # core score fills open slots, up to max_positions. Only the
        # STOP placement differs (blended_stop below).
        for sym in top20.index:
            if sym in positions or len(positions) >= effective_max_positions:
                continue
            if confirm_days is not None and candidate_streak.get(sym, 0) < confirm_days:
                continue
            if mom_cap is not None:
                mom_now = ranked.loc[sym, "mom_3m"] if sym in ranked.index else None
                if mom_now is not None and not pd.isna(mom_now) and mom_now > mom_cap:
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
            if mad_stop_enabled:
                # Explicit request ("keep entry logic as is.. only use the
                # Trail as support (bull)"): the MAD trail's lower band
                # becomes the stop outright when it's a sensible support
                # (bull regime, sitting below entry) -- falls back to the
                # plain ATR stop otherwise (e.g. entering a stock whose own
                # MAD trail isn't in a bull regime, or the band is missing/
                # above price), so every entry always gets SOME stop.
                mad = precomputed_mad.get(sym) if precomputed_mad else None
                m = mad.loc[date] if mad is not None and date in mad.index else None
                if (m is not None and m["regime"] == 1 and not pd.isna(m["lower"])
                     and m["lower"] < entry_price):
                    initial_stop = float(m["lower"])
                else:
                    initial_stop = entry_price - atr_stop_multiple * atr_now
            elif ema_stop_blend_enabled:
                ema13_ser = precomputed_ema13.get(sym)
                ema21_ser = precomputed_ema21.get(sym)
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
                "highest_close": entry_price, "qty": qty, "entry_date": date, "bars_held": 0,
            }

        equity_now = cash + sum(
            p["qty"] * float(long_candles[s].loc[date, "close"])
            for s, p in positions.items() if date in long_candles[s].index)
        equity_curve.append((date, equity_now))

    last_date = scan_dates[-1] if len(scan_dates) else None
    open_rows = []
    for sym, pos in positions.items():
        last_close = None
        df = long_candles.get(sym)
        if df is not None and last_date is not None and last_date in df.index:
            last_close = float(df.loc[last_date, "close"])
        unrealized_pnl = (last_close - pos["entry_price"]) * pos["qty"] if last_close is not None else None
        unrealized_ret_pct = (last_close / pos["entry_price"] - 1) * 100 if last_close is not None else None
        open_rows.append({
            "symbol": sym, "entry_date": pos["entry_date"], "entry_price": pos["entry_price"],
            "qty": pos["qty"], "stop": pos["stop"], "as_of_date": last_date,
            "last_close": last_close, "unrealized_pnl": unrealized_pnl,
            "unrealized_ret_pct": unrealized_ret_pct,
        })

    return (pd.DataFrame(trades),
           pd.DataFrame(equity_curve, columns=["date", "equity"]).set_index("date"),
           len(positions),
           pd.DataFrame(open_rows))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist-size", type=int, default=20)
    ap.add_argument("--max-positions", type=int, default=10)
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--atr-stop-multiple", type=float, default=2.0)
    ap.add_argument("--trailing-atr-multiple", type=float, default=3.0)
    ap.add_argument("--start-date", type=str, default=None)
    ap.add_argument("--days", type=int, default=1260)
    ap.add_argument("--rsi-min", type=float, default=None)
    ap.add_argument("--rsi-max", type=float, default=None)
    ap.add_argument("--core-ema-fast", type=int, default=None)
    ap.add_argument("--core-ema-slow", type=int, default=None)
    ap.add_argument("--mom-lookback-short", type=int, default=None)
    ap.add_argument("--mom-lookback-long", type=int, default=None)
    ap.add_argument("--sector-bonus-weight", type=float, default=None)
    ap.add_argument("--rsi-exit-gate", action="store_true")
    ap.add_argument("--weekly-monthly-gate", action="store_true")
    ap.add_argument("--regime-filter", action="store_true",
                    help="overrides regime_filter_enabled -> True -- halves max_positions "
                        "on any day NIFTY closes below its own regime_ema_period (200) EMA")
    ap.add_argument("--mom-method", choices=["fixed_lookback", "regression"], default=None,
                    help="overrides mom_method (default fixed_lookback)")
    ap.add_argument("--ema-stop-blend", action="store_true",
                    help="enable the EMA13/21-blended initial stop (default off -- "
                        "plain ATR stop)")
    ap.add_argument("--mad-stop", action="store_true",
                    help="use the MAD volatility trail's lower band as the stop "
                        "(initial + ongoing trail) instead of the ATR stop, falling "
                        "back to the ATR stop when the trail isn't a sensible support")
    ap.add_argument("--mad-med-len", type=int, default=None, help="overrides mt_med_len (default 30)")
    ap.add_argument("--mad-mad-len", type=int, default=None, help="overrides mt_mad_len (default 30)")
    ap.add_argument("--mad-dev-factor", type=float, default=None, help="overrides mt_dev_factor (default 2.0)")
    ap.add_argument("--mad-atr-floor-mult", type=float, default=None, help="overrides mt_atr_floor_mult (default 1.0)")
    ap.add_argument("--mad-slope-look", type=int, default=None, help="overrides mt_slope_look (default 3)")
    ap.add_argument("--target-rr", type=float, default=None,
                    help="adds a fixed target = entry + this * (entry - initial_stop), "
                        "e.g. 1.0 for a 1:1 risk/reward target")
    ap.add_argument("--time-stop-days", type=int, default=None,
                    help="force-close at close if --target-rr is set and not yet hit "
                        "within this many trading days, e.g. 20")
    ap.add_argument("--confirm-days", type=int, default=None,
                    help="require a stock to have stayed in the confirm-pool for this "
                        "many consecutive days before a NEW buy is allowed, e.g. 2")
    ap.add_argument("--confirm-pool-size", type=int, default=None,
                    help="the pool the confirm-days streak is measured against -- "
                        "defaults to --watchlist-size (top-20); pass 10 to require "
                        "top-10 persistence instead")
    ap.add_argument("--mom-cap", type=float, default=None,
                    help="block a NEW buy if entry-day mom_3m exceeds this (e.g. 80) -- "
                        "avoid already-extended/parabolic entries")
    ap.add_argument("--out-suffix", type=str, default="")
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

    cfg_overrides = {}
    if args.rsi_min is not None:
        cfg_overrides["rsi_min"] = args.rsi_min
    if args.rsi_max is not None:
        cfg_overrides["rsi_max"] = args.rsi_max
    if args.core_ema_fast is not None:
        cfg_overrides["ema_fast"] = args.core_ema_fast
    if args.core_ema_slow is not None:
        cfg_overrides["ema_slow"] = args.core_ema_slow
    if args.mom_lookback_short is not None:
        cfg_overrides["mom_lookback_days_short"] = args.mom_lookback_short
    if args.mom_lookback_long is not None:
        cfg_overrides["mom_lookback_days_long"] = args.mom_lookback_long
    if args.sector_bonus_weight is not None:
        cfg_overrides["sector_bonus_weight"] = args.sector_bonus_weight
    if args.rsi_exit_gate:
        cfg_overrides["rsi_exit_gate_enabled"] = True
        cfg_overrides["rsi_exit_max"] = 100.0
    if args.weekly_monthly_gate:
        cfg_overrides["weekly_monthly_gate_enabled"] = True
    if args.regime_filter:
        cfg_overrides["regime_filter_enabled"] = True
    if args.mom_method is not None:
        cfg_overrides["mom_method"] = args.mom_method
    if cfg_overrides:
        print(f"Overriding cfg: {cfg_overrides}")

    cfg = dict(config.STRATEGY)
    cfg.update(cfg_overrides)

    print("Precomputing daily indicators per symbol...")
    precomputed = {}
    for sym, df in long_candles.items():
        if not df.empty and len(df) >= cfg["ema_slow"]:
            precomputed[sym] = indicators.precompute_daily_series(df, cfg)

    precomputed_weekly_monthly = None
    precomputed_weekly_monthly_ok = None
    if cfg.get("weekly_monthly_gate_enabled", False):
        print("Precomputing weekly/monthly trend-ok per symbol (fast path, gate is ON)...")
        precomputed_weekly_monthly_ok = {}
        for sym, df in long_candles.items():
            if not df.empty:
                precomputed_weekly_monthly_ok[sym] = indicators.precompute_weekly_monthly_trend_ok(df, cfg)

    print("Precomputing EMA13/EMA21 per symbol (for stop blending)...")
    precomputed_ema13, precomputed_ema21 = {}, {}
    for sym, df in long_candles.items():
        if not df.empty:
            precomputed_ema13[sym] = indicators.ema(df["close"], 13)
            precomputed_ema21[sym] = indicators.ema(df["close"], 21)

    precomputed_mad = None
    if args.mad_stop:
        mad_cfg = dict(MAD_TRAIL_DEFAULTS)
        if args.mad_med_len is not None:
            mad_cfg["mt_med_len"] = args.mad_med_len
        if args.mad_mad_len is not None:
            mad_cfg["mt_mad_len"] = args.mad_mad_len
        if args.mad_dev_factor is not None:
            mad_cfg["mt_dev_factor"] = args.mad_dev_factor
        if args.mad_atr_floor_mult is not None:
            mad_cfg["mt_atr_floor_mult"] = args.mad_atr_floor_mult
        if args.mad_slope_look is not None:
            mad_cfg["mt_slope_look"] = args.mad_slope_look
        print(f"Precomputing MAD volatility trail per symbol (mad_cfg={mad_cfg})...")
        precomputed_mad = {}
        for sym, df in long_candles.items():
            if not df.empty and len(df) >= mad_cfg["mt_med_len"]:
                precomputed_mad[sym] = precompute_mad_trail(df, mad_cfg)

    trades, equity_curve, open_at_end, open_positions = run_backtest(
        scan_dates, long_candles, precomputed, precomputed_ema13, precomputed_ema21,
        bench, fundamentals_history, sector_candles, sector_membership,
        args.watchlist_size, args.max_positions, args.atr_stop_multiple,
        args.trailing_atr_multiple, initial_capital=args.capital,
        cfg_overrides=cfg_overrides, precomputed_weekly_monthly=precomputed_weekly_monthly,
        precomputed_weekly_monthly_ok=precomputed_weekly_monthly_ok,
        ema_stop_blend_enabled=args.ema_stop_blend, target_rr=args.target_rr,
        time_stop_days=args.time_stop_days, confirm_days=args.confirm_days,
        mom_cap=args.mom_cap, confirm_pool_size=args.confirm_pool_size,
        mad_stop_enabled=args.mad_stop, precomputed_mad=precomputed_mad)

    print(f"\n=== EMA-stop-blend={args.ema_stop_blend}, mad_stop={args.mad_stop}, "
         f"target_rr={args.target_rr}, "
         f"time_stop_days={args.time_stop_days}, confirm_days={args.confirm_days}, "
         f"confirm_pool_size={args.confirm_pool_size}, mom_cap={args.mom_cap} ===")
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
        by_reason = trades.groupby("reason").agg(
            n=("reason", "count"),
            win_rate=("ret_pct", lambda x: round(100 * (x > 0).mean(), 1)),
            avg_ret=("ret_pct", lambda x: round(x.mean(), 2)))
        print(f"\nBy exit reason:\n{by_reason.to_string()}")
    if not equity_curve.empty:
        years = (equity_curve.index[-1] - equity_curve.index[0]).days / 365.25
        final_equity = equity_curve["equity"].iloc[-1]
        cagr = (final_equity / args.capital) ** (1 / years) - 1 if years > 0 else 0
        running_max = equity_curve["equity"].cummax()
        dd = (equity_curve["equity"] / running_max - 1).min() * 100
        print(f"CAGR (annualized from {years:.2f}y window): {cagr*100:.2f}%")
        print(f"Max drawdown: {dd:.2f}%")
        print(f"Final equity: {final_equity:,.2f} (started {args.capital:,.0f})")
    tr_path = f"result/ema_stop_trades{args.out_suffix}.csv"
    eq_path = f"result/ema_stop_equity{args.out_suffix}.csv"
    op_path = f"result/ema_stop_open_positions{args.out_suffix}.csv"
    trades.to_csv(tr_path, index=False)
    equity_curve.to_csv(eq_path)
    open_positions.to_csv(op_path, index=False)
    if not open_positions.empty:
        print(f"\nOpen positions at end ({len(open_positions)}):")
        print(open_positions.to_string(index=False))
    print(f"Saved: {tr_path}, {eq_path}, {op_path}")


if __name__ == "__main__":
    main()
