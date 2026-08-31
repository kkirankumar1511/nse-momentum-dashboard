"""
Local-only 5-year position-managed backtest for swing_confluence_strategy.py
-- the user-supplied swing_trading_strategy.md 10-point EMA13/21/50
confluence spec, with OUR Pin Bar / Bullish Engulfing (quant_pattern.py's
current, tuned versions) and OUR ORIGINAL (pre-2026-08-27-rewrite) Morning
Star substituted for the spec's own candlestick detection. Explicit
request: "implement separately without touching this" -- a completely
separate script/module from run_quant_pattern_backtest_local.py and
quant_pattern.py, neither of which this file imports internals from.

Explicit request ("use same top 20 watchlist for new strategy"): reuses
the SAME Rank-1 production watchlist pipeline (backtest.rank_universe_asof)
as quant_pattern's own runner, top 20 by default -- this strategy's own
spec has no separate stock-selection layer of its own, so the existing
Rank-1 gate stands in for "your watchlisted assets" from the spec's
executive summary.

Run with: python scripts/run_swing_confluence_backtest_local.py --days 1260 --max-positions 5
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
import swing_confluence_strategy as sw

FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")
SECTOR_DATA_CACHE = os.path.join("cache", "sector_data.pkl")

# Same Rank-1 stock-selection config used by run_quant_pattern_backtest_local.py
# -- duplicated here (not imported) so this script stays fully standalone.
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


def run_position_managed_backtest(scan_dates, long_candles, sw_signals,
                                  precomputed_daily, precomputed_weekly_monthly_ok,
                                  bench, fundamentals_history, sector_candles,
                                  sector_membership, rank1_top_n, max_positions,
                                  max_new_per_day, initial_capital=1_000_000,
                                  pattern_filter=None):
    """Plain fixed stop/target bracket, equal-weight sizing -- same
    conventions as run_quant_pattern_backtest_local.py's own backtest, but
    a separate implementation (not imported) per the "implement
    separately" instruction. Explicit spec detail: only the top
    `max_new_per_day` highest-scoring new candidates are taken each day
    (the spec's own reference code slices `daily_score_board[:2]`).

    2026-08-27: a production-style ATR stop + 3x-ATR trailing stop (no
    fixed target) was tried and reverted -- a 5-year test showed it
    regressed badly (CAGR 8.00%->0.48%, win rate 40.9%->31.9%), mainly
    by widening Pin Bar's and Bullish Engulfing's stops far past their
    own tight pattern-based levels and removing the fixed target that
    was capturing gains those trades wouldn't have held onto otherwise.
    Back to the pattern's own low-based stop + fixed 1:2 target, per
    explicit confirmation ("Revert to fixed 1:2 bracket")."""
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
                    "qty": pos["qty"], "reason": reason, "pattern": pos["pattern"],
                    "score": pos["score"], "pnl": pnl, "ret_pct": ret_pct,
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
            sig = sw_signals.get(sym)
            if sig is None or date not in sig.index:
                continue
            row = sig.loc[date]
            if bool(row["confirmed_entry"]) and (pattern_filter is None or row["pattern_name"] == pattern_filter):
                candidates.append((sym, row))
        candidates.sort(key=lambda x: -x[1]["score"])
        candidates = candidates[:max_new_per_day]

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
                "pattern": row["pattern_name"], "score": row["score"],
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
    ap.add_argument("--max-new-per-day", type=int, default=2,
                    help="cap on new entries taken per day, matching the spec's own daily_score_board[:2]")
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--pattern-filter", type=str, default=None,
                    help="restrict entries to only this pattern_name, e.g. 'Pin Bar' -- "
                        "for isolated per-pattern A/B tests")
    args = ap.parse_args()

    sw_cfg = dict(sw.SWING_DEFAULTS)

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

    print("Precomputing swing_confluence signals per symbol...")
    sw_signals: dict[str, pd.DataFrame] = {}
    for sym, df in long_candles.items():
        if df.empty or len(df) < max(sw_cfg["sw_ema_trend"], 60):
            continue
        sw_signals[sym] = sw.precompute_swing_signals(df, sw_cfg)

    print("Precomputing Rank-1 daily/weekly/monthly indicators per symbol...")
    precomputed_daily = {}
    precomputed_weekly_monthly_ok = {}
    for sym, df in long_candles.items():
        if not df.empty and len(df) >= RANK1_CFG["ema_slow"]:
            precomputed_daily[sym] = indicators.precompute_daily_series(df, RANK1_CFG)
        if not df.empty:
            precomputed_weekly_monthly_ok[sym] = indicators.precompute_weekly_monthly_trend_ok(
                df, RANK1_CFG)

    if args.pattern_filter is not None:
        print(f"Restricting entries to pattern_name == {args.pattern_filter!r} (from --pattern-filter).")

    if args.max_positions is not None:
        trades, equity_curve, open_at_end = run_position_managed_backtest(
            scan_dates, long_candles, sw_signals, precomputed_daily,
            precomputed_weekly_monthly_ok, bench, fundamentals_history,
            sector_candles, sector_membership, args.rank1_top_n,
            args.max_positions, args.max_new_per_day, initial_capital=args.capital,
            pattern_filter=args.pattern_filter)

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
        trades.to_csv("result/swing_confluence_positionmanaged_trades.csv", index=False)
        equity_curve.to_csv("result/swing_confluence_positionmanaged_equity.csv")
        print("Saved: result/swing_confluence_positionmanaged_trades.csv, "
             "result/swing_confluence_positionmanaged_equity.csv")
        return

    print("(no --max-positions given: pass e.g. --max-positions 5 to run a real backtest)")


if __name__ == "__main__":
    main()
