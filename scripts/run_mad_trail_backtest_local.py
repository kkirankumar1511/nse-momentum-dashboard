"""
MAD Volatility Trail swing strategy, layered on our existing top-20
momentum watchlist. Explicit request: "implement in the best way on our
top 20 watchlist... suggest changes required as per our watchlist."

Design (mirrors run_ema_stop_backtest_local.py's day-loop structure):
  1. WHICH stocks are eligible each day: the unchanged core momentum
     ranking (bt.rank_universe_asof with the live/PDF cfg, exactly as
     every other test this session) -- top N (--watchlist-size) gate-
     passers by score.
  2. WHEN to buy: among that watchlist, a stock must show a fresh MAD-
     trail bull flip (regime just flipped +1, within --entry-window bars)
     with the median sloping up -- mad_trail_strategy.precompute_mad_trail.
  3. STOP: the trail's own one-sided ratcheting lower band (recomputed
     daily from the precomputed series, not a separate ATR-multiple stop)
     -- replaces this codebase's usual atr_stop_multiple mechanism
     entirely for this strategy.
  4. EXIT: bear flip (regime -1) at that day's close. --rank-exit
     additionally drops a position that fell out of the top-20 core-score
     keep zone (screener.sell_check), off by default so the first test is
     a CLEAN read on the MAD trail's own signal quality alone.

Run with: python scripts/run_mad_trail_backtest_local.py --start-date 2021-01-01
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


def run_backtest(scan_dates, long_candles, precomputed, precomputed_mad,
                 bench, fundamentals_history, sector_candles, sector_membership,
                 watchlist_size, max_positions, initial_capital=1_000_000,
                 rank_exit_enabled=False, cfg_overrides=None,
                 precomputed_weekly_monthly=None, entry_window=5):
    cash = initial_capital
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    equity_curve: list[tuple] = []
    cfg = dict(config.STRATEGY)
    if cfg_overrides:
        cfg.update(cfg_overrides)

    for date in scan_dates:
        # 1) stop check (gap-aware, same fill rule as every other script
        # this session) -- pos["stop"] is refreshed from the precomputed
        # trail's own lower band each day (see step 1b), so this is
        # effectively "did today's low pierce the current trail level".
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
            else:
                mad = precomputed_mad.get(sym)
                if mad is not None and date in mad.index and bool(mad.loc[date, "flip_bear"]):
                    exit_price = float(row["close"])
                    reason = "bear_flip"
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

        # 1b) trail ratchet -- the precomputed lower band is already a
        # proper one-sided ratchet (mad_trail_strategy.precompute_mad_
        # trail), so this is just "adopt today's value if it's higher",
        # a defensive guard against ever moving the stop down.
        for sym, pos in list(positions.items()):
            mad = precomputed_mad.get(sym)
            if mad is None or date not in mad.index:
                continue
            new_stop = float(mad.loc[date, "lower"])
            if new_stop > pos["stop"]:
                pos["stop"] = new_stop

        # 2) rank with the UNCHANGED core score -- decides WHICH stocks
        # are eligible, same as every other script this session.
        ranked = bt.rank_universe_asof(
            long_candles, bench, date, cfg,
            fundamentals_history, {}, sector_candles, sector_membership,
            long_candles, precomputed, None, precomputed_weekly_monthly)
        if ranked.empty:
            equity_curve.append((date, cash + sum(
                p["qty"] * float(long_candles[s].loc[date, "close"])
                for s, p in positions.items() if date in long_candles[s].index)))
            continue
        candidates = ranked[ranked["all_gates"]].sort_values("score", ascending=False)
        top20 = candidates.head(watchlist_size)
        keep_zone = set(top20.index)

        # 2b) optional rank-exit -- off by default (see module docstring)
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

        # 3) entries: among today's top-20, a stock qualifies for a NEW
        # buy only if it's showing a fresh MAD-trail bull signal.
        for sym in top20.index:
            if sym in positions or len(positions) >= max_positions:
                continue
            df = long_candles.get(sym)
            mad = precomputed_mad.get(sym)
            if df is None or mad is None or date not in df.index or date not in mad.index:
                continue
            m = mad.loc[date]
            if not (m["regime"] == 1 and m["bars_since_bull_flip"] <= entry_window and bool(m["slope_up"])):
                continue
            stop = float(m["lower"])
            equity_now = cash + sum(
                p["qty"] * float(long_candles[s].loc[date, "close"])
                for s, p in positions.items() if date in long_candles[s].index)
            slot_capital = equity_now / max_positions
            entry_price = float(df.loc[date, "close"])
            if entry_price <= stop:
                continue  # degenerate/inverted band -- skip rather than buy with zero/negative risk room
            qty = int(slot_capital / entry_price)
            if qty <= 0 or qty * entry_price > cash:
                continue
            cash -= qty * entry_price
            positions[sym] = {
                "entry_price": entry_price, "stop": stop,
                "qty": qty, "entry_date": date,
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
    ap.add_argument("--watchlist-size", type=int, default=20)
    ap.add_argument("--max-positions", type=int, default=10)
    ap.add_argument("--capital", type=float, default=1_000_000)
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
    ap.add_argument("--rank-exit", action="store_true",
                    help="also drop a position that fell out of the top-20 core-score "
                        "keep zone (default off -- pure MAD-trail exits only)")
    ap.add_argument("--entry-window", type=int, default=5,
                    help="a bull flip must have happened within this many bars (default 5)")
    ap.add_argument("--med-len", type=int, default=None, help="overrides mt_med_len (default 30)")
    ap.add_argument("--mad-len", type=int, default=None, help="overrides mt_mad_len (default 30)")
    ap.add_argument("--dev-factor", type=float, default=None, help="overrides mt_dev_factor (default 2.0)")
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
    if cfg_overrides:
        print(f"Overriding core cfg: {cfg_overrides}")

    mad_cfg = dict(MAD_TRAIL_DEFAULTS)
    if args.med_len is not None:
        mad_cfg["mt_med_len"] = args.med_len
    if args.mad_len is not None:
        mad_cfg["mt_mad_len"] = args.mad_len
    if args.dev_factor is not None:
        mad_cfg["mt_dev_factor"] = args.dev_factor

    cfg = dict(config.STRATEGY)
    cfg.update(cfg_overrides)

    print("Precomputing daily indicators per symbol (for core ranking)...")
    precomputed = {}
    for sym, df in long_candles.items():
        if not df.empty and len(df) >= cfg["ema_slow"]:
            precomputed[sym] = indicators.precompute_daily_series(df, cfg)

    precomputed_weekly_monthly = None
    if cfg.get("weekly_monthly_gate_enabled", False):
        print("Precomputing weekly/monthly bars per symbol (gate is ON)...")
        precomputed_weekly_monthly = {}
        for sym, df in long_candles.items():
            if not df.empty:
                precomputed_weekly_monthly[sym] = indicators.precompute_weekly_monthly_bars(df["close"])

    print("Precomputing MAD volatility trail per symbol...")
    precomputed_mad = {}
    for sym, df in long_candles.items():
        if not df.empty and len(df) >= mad_cfg["mt_med_len"]:
            precomputed_mad[sym] = precompute_mad_trail(df, mad_cfg)

    trades, equity_curve, open_at_end = run_backtest(
        scan_dates, long_candles, precomputed, precomputed_mad,
        bench, fundamentals_history, sector_candles, sector_membership,
        args.watchlist_size, args.max_positions, initial_capital=args.capital,
        rank_exit_enabled=args.rank_exit, cfg_overrides=cfg_overrides,
        precomputed_weekly_monthly=precomputed_weekly_monthly,
        entry_window=args.entry_window)

    print(f"\n=== MAD trail on top-{args.watchlist_size} watchlist, "
         f"max {args.max_positions} positions, rank_exit={args.rank_exit} ===")
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
        print(f"\nBy exit reason:\n{trades.groupby('reason')['ret_pct'].agg(['count','mean']).to_string()}")
    if not equity_curve.empty:
        years = (equity_curve.index[-1] - equity_curve.index[0]).days / 365.25
        final_equity = equity_curve["equity"].iloc[-1]
        cagr = (final_equity / args.capital) ** (1 / years) - 1 if years > 0 else 0
        running_max = equity_curve["equity"].cummax()
        dd = (equity_curve["equity"] / running_max - 1).min() * 100
        print(f"CAGR (annualized from {years:.2f}y window): {cagr*100:.2f}%")
        print(f"Max drawdown: {dd:.2f}%")
        print(f"Final equity: {final_equity:,.2f} (started {args.capital:,.0f})")
    tr_path = f"result/mad_trail_trades{args.out_suffix}.csv"
    eq_path = f"result/mad_trail_equity{args.out_suffix}.csv"
    trades.to_csv(tr_path, index=False)
    equity_curve.to_csv(eq_path)
    print(f"Saved: {tr_path}, {eq_path}")


if __name__ == "__main__":
    main()
