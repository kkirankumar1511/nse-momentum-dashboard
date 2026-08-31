"""
Ad-hoc: builds a day-by-day archive report from a saved ema21-touch
equity + trades CSV pair -- for every trading day: total portfolio
equity, how many positions are currently held and their approximate
cost-basis value, and any trade(s) that closed that day with win/loss
and realized P&L (plus running cumulative realized P&L). Local-only,
read-only -- does not alter any strategy code or run a new backtest.

Run with:
  python scripts/build_daily_report.py <equity_csv> <trades_csv> <out_csv>
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

EQUITY_CSV = sys.argv[1] if len(sys.argv) > 1 else \
    "result/backtest_equity_ema21touch_new_latestlow_5yr_latestlow_rsi50.csv"
TRADES_CSV = sys.argv[2] if len(sys.argv) > 2 else \
    "result/backtest_trades_ema21touch_new_latestlow_5yr_latestlow_rsi50.csv"
OUT_CSV = sys.argv[3] if len(sys.argv) > 3 else \
    "result/daily_report_ema21touch_new_latestlow_5yr.csv"

equity = pd.read_csv(EQUITY_CSV, index_col=0, parse_dates=True)["equity"]
trades = pd.read_csv(TRADES_CSV, parse_dates=["entry_date", "exit_date"])

# Build per-day: which positions are open at END OF DAY. The backtest's
# own day loop processes stop/target closes BEFORE new entries within a
# single day (see backtest_triggered.py's step ordering), so a position
# that exits on day D is already cash by end of day D, while a new entry
# on day D IS held by end of day D -- hence entry_date <= day < exit_date
# (exit_date itself excluded), not <=. Using <= on both ends double-counts
# same-day close+reopen slot churn as if both were simultaneously held
# (caught via a real check: 2024-01-19 showed BHEL closing the same day
# HAL and BEL opened, naively reporting 6 open against a 5-position cap
# that was never actually violated).
rows = []
cum_realized_pnl = 0.0
for date, eq in equity.items():
    open_mask = (trades["entry_date"] <= date) & (trades["exit_date"] > date)
    open_today = trades[open_mask]
    n_open = len(open_today)
    holdings_cost_basis = float((open_today["entry_price"] * open_today["qty"]).sum())

    closed_today = trades[trades["exit_date"] == date]
    n_wins_today = int((closed_today["pnl"] > 0).sum())
    n_losses_today = int((closed_today["pnl"] <= 0).sum())
    daily_realized_pnl = float(closed_today["pnl"].sum())
    cum_realized_pnl += daily_realized_pnl

    closed_symbols = "; ".join(
        f"{r.symbol}:{'WIN' if r.pnl > 0 else 'LOSS'}({r.pnl:+.0f})"
        for r in closed_today.itertuples())

    rows.append({
        "date": date.date(), "equity": round(eq, 2),
        "open_positions": n_open, "holdings_cost_basis": round(holdings_cost_basis, 2),
        "trades_closed_today": len(closed_today),
        "wins_today": n_wins_today, "losses_today": n_losses_today,
        "daily_realized_pnl": round(daily_realized_pnl, 2),
        "cumulative_realized_pnl": round(cum_realized_pnl, 2),
        "closed_trade_detail": closed_symbols,
    })

report = pd.DataFrame(rows)
report.to_csv(OUT_CSV, index=False)
print(f"Saved {len(report)} daily rows to {OUT_CSV}")
print(f"\nTotal trading days: {len(report)}")
print(f"Days with at least one closed trade: {(report['trades_closed_today']>0).sum()}")
print(f"Total wins: {report['wins_today'].sum()}, total losses: {report['losses_today'].sum()}")
print(f"Final cumulative realized P&L: {report['cumulative_realized_pnl'].iloc[-1]:,.0f}")
print(f"Final equity: {report['equity'].iloc[-1]:,.0f}")
print(f"Max open positions on any day: {report['open_positions'].max()}")
print(f"\nFirst 5 rows:\n{report.head()}")
print(f"\nLast 5 rows:\n{report.tail()}")
