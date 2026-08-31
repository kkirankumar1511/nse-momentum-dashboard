"""
Local-only position-managed backtest for ema_stack_pullback_strategy.py --
EMA13/21/50/200 stack alignment + RSI-crossing signal candle (prev RSI<60,
signal RSI>=59.50), 3-candle close-above-signal confirmation window, entry
at that candle's close, stop = EMA13 with a small threshold, fixed 1:2
target. A separate script/module from quant_pattern.py and
swing_confluence_strategy.py.

Run with: python scripts/run_ema_stack_pullback_backtest_local.py --days 21 --max-positions 10
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
import ema_stack_pullback_strategy as es

FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")
SECTOR_DATA_CACHE = os.path.join("cache", "sector_data.pkl")

# Same Rank-1 stock-selection config used by the other two runners --
# duplicated here (not imported) so this script stays fully standalone.
RANK1_CFG = dict(config.STRATEGY)
RANK1_CFG.update({
    "rsi_min": 45.0, "rsi_max": 75.0,
    "ema_fast": 50, "ema_slow": 200,
    "mom_lookback_days_short": 63, "mom_lookback_days_long": 126,
    "skip_recent_days": 5,
    "atr_stop_multiple": 2.0, "trailing_stop_enabled": True, "trailing_atr_multiple": 3.0,
    "risk_per_trade_pct": 0.5, "max_positions": 10, "history_days": 1200,
    "rsi_exit_gate_enabled": True, "rsi_exit_max": 100.0,
    "weekly_monthly_gate_enabled": True,
    "advanced_equal_weight_sizing": True, "equal_weight_tolerance_pct": 0.20,
    "fundamental_gate_enabled": True, "fundamental_bonus_weight": 0.5,
    "min_fundamental_score": 50.0, "near_high_threshold": 0.85,
    "sector_bonus_weight": 0.5, "sector_diversification_enabled": False,
    "resistance_zone_weight": 0.0, "regime_filter_enabled": False,
})


def run_position_managed_backtest(scan_dates, long_candles, es_signals,
                                  precomputed_daily, precomputed_weekly_monthly_ok,
                                  bench, fundamentals_history, sector_candles,
                                  sector_membership, rank1_top_n, max_positions,
                                  initial_capital=1_000_000):
    """Fixed stop/target bracket (EMA13-based stop, fixed 1:2 target,
    both frozen at entry), equal-weight sizing -- same gap-aware
    stop-fill convention as backtest.py's own production run_backtest."""
    cash = initial_capital
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    equity_curve: list[tuple] = []

    for date in scan_dates:
        for sym in list(positions.keys()):
            df = long_candles.get(sym)
            if df is None or date not in df.index:
                continue
            row = df.loc[date]
            pos = positions[sym]
            exit_price = None
            reason = None
            if row["low"] <= pos["stop"]:
                exit_price, reason = pos["stop"], "stop"
            elif row["high"] >= pos["target"]:
                exit_price, reason = pos["target"], "target"
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

        ranked = bt.rank_universe_asof(
            long_candles, bench, date, RANK1_CFG,
            fundamentals_history, {}, sector_candles, sector_membership,
            long_candles, precomputed_daily, None, None, precomputed_weekly_monthly_ok)
        watchlist = []
        if not ranked.empty:
            passers = ranked[ranked["all_gates"]].sort_values("score", ascending=False)
            watchlist = list(passers.head(rank1_top_n).index)

        candidates = []
        for sym in watchlist:
            if sym in positions:
                continue
            sig = es_signals.get(sym)
            if sig is None or date not in sig.index:
                continue
            row = sig.loc[date]
            if bool(row["confirmed_entry"]):
                candidates.append((sym, row))

        for sym, row in candidates:
            if len(positions) >= max_positions:
                break
            equity_now = cash + sum(
                p["qty"] * float(long_candles[s].loc[date, "close"])
                for s, p in positions.items() if date in long_candles[s].index)
            slot_capital = equity_now / max_positions
            entry_price = float(row["entry_price"])
            qty = int(slot_capital / entry_price)
            if qty <= 0 or qty * entry_price > cash:
                continue
            cash -= qty * entry_price
            positions[sym] = {
                "entry_price": entry_price, "stop": float(row["stop"]),
                "target": float(row["target"]), "qty": qty, "entry_date": date,
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
    ap.add_argument("--rank1-top-n", type=int, default=20,
                    help="how many of each day's gate-passers count as the watchlist (default 20)")
    ap.add_argument("--max-positions", type=int, default=None,
                    help="if given, runs a real position-managed backtest instead of just listing signals")
    ap.add_argument("--capital", type=float, default=1_000_000)
    args = ap.parse_args()

    es_cfg = dict(es.EMA_STACK_DEFAULTS)

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
    scan_dates = all_dates[-args.days:]
    print(f"\nScanning {len(scan_dates)} trading days: "
         f"{scan_dates[0].date()} to {scan_dates[-1].date()}\n")

    print("Precomputing ema_stack_pullback signals per symbol...")
    es_signals: dict[str, pd.DataFrame] = {}
    for sym, df in long_candles.items():
        if df.empty or len(df) < max(es_cfg["es_ema_slow"], 60):
            continue
        es_signals[sym] = es.precompute_ema_stack_signals(df, es_cfg)

    print("Precomputing Rank-1 daily/weekly/monthly indicators per symbol...")
    precomputed_daily = {}
    precomputed_weekly_monthly_ok = {}
    for sym, df in long_candles.items():
        if not df.empty and len(df) >= RANK1_CFG["ema_slow"]:
            precomputed_daily[sym] = indicators.precompute_daily_series(df, RANK1_CFG)
        if not df.empty:
            precomputed_weekly_monthly_ok[sym] = indicators.precompute_weekly_monthly_trend_ok(
                df, RANK1_CFG)

    if args.max_positions is not None:
        trades, equity_curve, open_at_end = run_position_managed_backtest(
            scan_dates, long_candles, es_signals, precomputed_daily,
            precomputed_weekly_monthly_ok, bench, fundamentals_history,
            sector_candles, sector_membership, args.rank1_top_n,
            args.max_positions, initial_capital=args.capital)

        print(f"\n=== Position-managed backtest: top {args.rank1_top_n} watchlist, "
             f"max {args.max_positions} concurrent positions ===")
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
        trades.to_csv("result/ema_stack_pullback_positionmanaged_trades.csv", index=False)
        equity_curve.to_csv("result/ema_stack_pullback_positionmanaged_equity.csv")
        print("Saved: result/ema_stack_pullback_positionmanaged_trades.csv, "
             "result/ema_stack_pullback_positionmanaged_equity.csv")
        return

    print("(no --max-positions given: pass e.g. --max-positions 10 to run a real backtest)")


if __name__ == "__main__":
    main()
