"""
Parameter sweep for the MAD-stop mechanism against the REAL production engine
(backtest.run_backtest), loading candles/bench/sector/fundamentals ONCE and
reusing them across every combo to avoid redundant I/O.

Goal: find whether tightening (or loosening) the MAD trail's dev_factor /
atr_floor_mult reduces the severity of stop-triggered losses and/or overall
max drawdown, without materially hurting CAGR via whipsaw.

Baseline config matches this session's validated MAD-stop numbers:
regime_filter ON, weekly_monthly_gate ON, entry_confirm OFF, start 2021-01-01.

Read-only. Local-only. Does not touch cache/state.db or config.py.
"""
from __future__ import annotations

import datetime as dt
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import backtest as bt
import config

FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")
SECTOR_DATA_CACHE = os.path.join("cache", "sector_data.pkl")

START_DATE = dt.date(2021, 1, 1)

# (dev_factor, atr_floor_mult) grid -- med_len/mad_len held at 21 (validated value)
GRID = list(itertools.product([1.5, 2.0, 2.5], [1.0, 1.5, 2.0]))


def summarize_trades(trades: pd.DataFrame) -> dict:
    stop = trades[trades["reason"] == "stop"]
    losers_stop = stop[stop["pnl"] < 0]
    return {
        "n_stop": len(stop),
        "stop_win_rate": round((stop["pnl"] > 0).mean(), 3) if len(stop) else None,
        "stop_avg_loss_pct": round(losers_stop["ret_pct"].mean(), 2) if len(losers_stop) else None,
        "stop_worst_loss_pct": round(losers_stop["ret_pct"].min(), 2) if len(losers_stop) else None,
        "stop_total_pnl": round(stop["pnl"].sum(), 0),
    }


def main():
    cfg_base = dict(config.STRATEGY)
    cfg_base["regime_filter_enabled"] = True
    cfg_base["weekly_monthly_gate_enabled"] = True
    cfg_base["entry_confirm_days"] = 0
    cfg_base["mad_stop_enabled"] = True
    cfg_base["mad_stop_med_len"] = 21
    cfg_base["mad_stop_mad_len"] = 21

    fundamentals_history = None
    if os.path.exists(FUNDAMENTALS_HISTORY_CACHE):
        fundamentals_history = pd.read_pickle(FUNDAMENTALS_HISTORY_CACHE)["history"]

    sector_membership = sector_candles = None
    if os.path.exists(SECTOR_DATA_CACHE):
        _sd = pd.read_pickle(SECTOR_DATA_CACHE)
        sector_membership, sector_candles = _sd["sector_membership"], _sd["sector_candles"]

    end_date = dt.date.today() - dt.timedelta(days=1)
    print("Loading candles (once, reused across sweep)...")
    long_candles = bt.load_long_history_cached(config.UNIVERSE, end_date=end_date)
    candles = long_candles
    bench = bt._tz_naive(pd.read_csv(os.path.join(bt.LONG_CACHE_DIR, "_NIFTY.csv"),
                                     index_col=0, parse_dates=True))
    print(f"Loaded {len(candles)} symbols.\n")

    rows = []
    for dev_factor, atr_floor_mult in GRID:
        cfg = dict(cfg_base)
        cfg["mad_stop_dev_factor"] = dev_factor
        cfg["mad_stop_atr_floor_mult"] = atr_floor_mult
        tag = f"dev{dev_factor}_floor{atr_floor_mult}"
        print(f"--- Running {tag} ---")
        t0 = dt.datetime.now()
        res = bt.run_backtest(
            candles, bench, cfg, initial_capital=1_000_000,
            rebalance="MS", fundamentals_history=fundamentals_history,
            sector_candles=sector_candles, sector_membership=sector_membership,
            long_candles=long_candles, start_date=START_DATE)
        dt_sec = (dt.datetime.now() - t0).total_seconds()
        m = res["metrics"]
        row = {
            "dev_factor": dev_factor, "atr_floor_mult": atr_floor_mult,
            "cagr": m.get("CAGR %"), "max_dd": m.get("Max drawdown %"),
            "sharpe": m.get("Sharpe"), "win_rate": m.get("Win rate %"),
            "profit_factor": m.get("Profit factor"),
            "n_trades": len(res["trades"]),
            "sec": round(dt_sec, 1),
        }
        row.update(summarize_trades(res["trades"]))
        rows.append(row)
        print(row)
        res["trades"].to_csv(f"result/mad_sweep_trades_{tag}.csv", index=False)
        res["equity_curve"].rename("equity").to_csv(f"result/mad_sweep_equity_{tag}.csv")
        print()

    out = pd.DataFrame(rows)
    out.to_csv("result/mad_sweep_summary.csv", index=False)
    print("\n=== SWEEP SUMMARY ===")
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
