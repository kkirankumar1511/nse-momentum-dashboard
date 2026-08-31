"""
Ad-hoc, read-only report: year-by-year breakdown of a saved backtest run
(equity + trades CSV pair from run_ema21touch_backtest_local.py or
run_triggered_backtest_local.py). Not a strategy change.

Run with: python scripts/yearwise_summary.py <equity.csv> <trades.csv>
"""
from __future__ import annotations

import sys

import pandas as pd


def main():
    equity_path, trades_path = sys.argv[1], sys.argv[2]

    equity = pd.read_csv(equity_path, index_col=0, parse_dates=True)["equity"]
    trades = pd.read_csv(trades_path, parse_dates=["entry_date", "exit_date"])

    print(f"=== Year-wise breakdown: {trades_path} ===")
    rows = []
    for year, eq_year in equity.groupby(equity.index.year):
        start_eq = eq_year.iloc[0]
        end_eq = eq_year.iloc[-1]
        ret_pct = (end_eq / start_eq - 1) * 100
        running_max = eq_year.cummax()
        dd_pct = ((eq_year - running_max) / running_max * 100).min()

        yr_trades = trades[trades["exit_date"].dt.year == year]
        n = len(yr_trades)
        win_rate = (yr_trades["pnl"] > 0).mean() * 100 if n else float("nan")
        wins = yr_trades[yr_trades["pnl"] > 0]["pnl"].sum()
        losses = -yr_trades[yr_trades["pnl"] < 0]["pnl"].sum()
        profit_factor = (wins / losses) if losses > 0 else float("inf") if wins > 0 else float("nan")
        avg_hold = yr_trades["holding_days"].mean() if n else float("nan")

        rows.append({
            "year": year, "start_equity": round(start_eq, 0), "end_equity": round(end_eq, 0),
            "return_%": round(ret_pct, 2), "max_dd_%": round(dd_pct, 2),
            "trades": n, "win_rate_%": round(win_rate, 1) if n else None,
            "profit_factor": round(profit_factor, 2) if n else None,
            "avg_hold_days": round(avg_hold, 1) if n else None,
        })

    df = pd.DataFrame(rows).set_index("year")
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(df)


if __name__ == "__main__":
    main()
