"""
Tests adding a HARD CAP on the MAD-stop's initial stop distance (e.g. never
let the initial stop sit more than X% below entry, regardless of what the
MAD trail's lower band says) -- motivated by a real case (IEX 2021-12-10)
where the trail's slow median hadn't caught up to a one-day spike entry,
giving an initial stop 21% below entry.

Monkeypatches backtest._initial_stop to apply the cap; does NOT modify
backtest.py itself. Reuses loaded candles across all combos.

Tests the cap against two floor settings: the current shipped default
(atr_floor_mult=2.0) and the sweep-recommended tighter one (=1.0), each
with cap in {None, 15%, 12%, 10%, 8%}.

Read-only. Local-only.
"""
from __future__ import annotations

import datetime as dt
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

_orig_initial_stop = bt._initial_stop
_CAP_PCT = {"value": None}  # mutable box so the patched fn can read the current sweep value


def _capped_initial_stop(entry_price, atr_now, cfg, sym, date, precomputed_mad):
    stop = _orig_initial_stop(entry_price, atr_now, cfg, sym, date, precomputed_mad)
    cap = _CAP_PCT["value"]
    if cap is not None:
        floor_stop = entry_price * (1 - cap)
        stop = max(stop, floor_stop)
    return stop


bt._initial_stop = _capped_initial_stop


def summarize_trades(trades: pd.DataFrame) -> dict:
    stop = trades[trades["reason"] == "stop"]
    losers_stop = stop[stop["pnl"] < 0]
    return {
        "n_stop": len(stop),
        "stop_avg_loss_pct": round(losers_stop["ret_pct"].mean(), 2) if len(losers_stop) else None,
        "stop_worst_loss_pct": round(losers_stop["ret_pct"].min(), 2) if len(losers_stop) else None,
    }


def main():
    cfg_base = dict(config.STRATEGY)
    cfg_base["regime_filter_enabled"] = True
    cfg_base["weekly_monthly_gate_enabled"] = True
    cfg_base["entry_confirm_days"] = 0
    cfg_base["mad_stop_enabled"] = True
    cfg_base["mad_stop_med_len"] = 21
    cfg_base["mad_stop_mad_len"] = 21
    cfg_base["mad_stop_dev_factor"] = 2.0

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
    for floor_mult in [2.0, 1.0]:
        for cap in [None, 0.15, 0.12, 0.10, 0.08]:
            cfg = dict(cfg_base)
            cfg["mad_stop_atr_floor_mult"] = floor_mult
            _CAP_PCT["value"] = cap
            tag = f"floor{floor_mult}_cap{cap}"
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
                "floor_mult": floor_mult, "cap_pct": cap,
                "cagr": m.get("CAGR %"), "max_dd": m.get("Max drawdown %"),
                "sharpe": m.get("Sharpe"), "win_rate": m.get("Win rate %"),
                "profit_factor": m.get("Profit factor"),
                "n_trades": len(res["trades"]), "sec": round(dt_sec, 1),
            }
            row.update(summarize_trades(res["trades"]))
            rows.append(row)
            print(row)
            res["trades"].to_csv(f"result/mad_capped_trades_{tag}.csv", index=False)
            print()

    out = pd.DataFrame(rows)
    out.to_csv("result/mad_capped_summary.csv", index=False)
    print("\n=== CAP SWEEP SUMMARY ===")
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
