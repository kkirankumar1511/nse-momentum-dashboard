"""
Local-only diagnostic export: for each trading day in a window, the top-N
gate-passing stocks by score (i.e. what the triggered-entry engine's
watchlist would have held that day). Not wired into dashboard.py, not
deployed -- pure local research/reporting script.

Run with:  python scripts/export_daily_watchlist.py --years 0.25 --top-n 10
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config
from backtest import load_candles_cached, load_long_history_cached, rank_universe_asof

FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")
SECTOR_DATA_CACHE = os.path.join("cache", "sector_data.pkl")

# Same filter config as the current triggered-engine tests (scripts/
# run_triggered_backtest_local.py's FILTER_CFG_OVERRIDE) -- kept in sync
# manually since this is a separate, occasional reporting script.
FILTER_CFG_OVERRIDE: dict = {
    "rsi_min": 60,
    "rsi_max": 100,  # RSI is bounded [0,100] -- 100 means no effective upper cap
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=0.25)
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--out", type=str, default="daily_watchlist.csv")
    args = ap.parse_args()

    fundamentals_history = None
    if os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
        fundamentals_history = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)["history"]

    sector_membership = sector_candles = None
    if os.path.exists(SECTOR_DATA_CACHE):
        _sd = pd.read_pickle(SECTOR_DATA_CACHE)
        sector_membership, sector_candles = _sd["sector_membership"], _sd["sector_candles"]

    long_candles = load_long_history_cached(
        config.UNIVERSE, end_date=dt.date.today() - dt.timedelta(days=1))

    days = 6100
    candles, bench = load_candles_cached(config.UNIVERSE, days, offline=True)

    all_dates = bench.index.sort_values()
    requested_start = dt.date.today() - dt.timedelta(days=int(args.years * 365))
    dates = all_dates[all_dates >= pd.Timestamp(requested_start)]
    print(f"Exporting top-{args.top_n} watchlist for {len(dates)} trading days, "
         f"{dates[0].date()} to {dates[-1].date()}.")

    cfg = {**config.STRATEGY, **FILTER_CFG_OVERRIDE}
    score_cache: dict = {}
    rows = []
    n_dates = len(dates)
    for i, date in enumerate(dates):
        if i % 10 == 0:
            print(f"  {i + 1}/{n_dates} ({date.date()})...")
        ranked = rank_universe_asof(candles, bench, date, cfg,
                                    fundamentals_history, score_cache,
                                    sector_candles, sector_membership,
                                    long_candles, None, None, None)
        if ranked.empty:
            continue
        candidates = ranked[ranked["all_gates"]]
        top = candidates.head(args.top_n)
        for rank, (sym, row) in enumerate(top.iterrows(), start=1):
            rows.append({
                "date": date.date(),
                "rank": rank,
                "symbol": sym,
                "score": round(float(row.get("score", float("nan"))), 3),
                "price": round(float(row.get("price", float("nan"))), 2),
                "sector": row.get("top_sector"),
                "rsi": round(float(row.get("rsi", float("nan"))), 1),
                "pct_52w_high": round(float(row.get("pct_52w_high", float("nan"))), 3),
            })

    out_df = pd.DataFrame(rows)
    out_df.to_csv(args.out, index=False)
    print(f"\nSaved {len(out_df)} rows ({n_dates} days x up to {args.top_n}) to {args.out}")


if __name__ == "__main__":
    main()
