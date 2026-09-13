"""Seeds today's sandbox intraday_state_sandbox.db with a realistic
demo scenario -- 2 candidates, one with a closed (target + squareoff)
position, one with a still-active signal -- so the Intraday Dashboard/
Tradebook pages show the full feature set instead of empty states.
Only ever touches the SANDBOX db (cache/intraday_state_sandbox.db),
never the real cache/intraday_state.db.

Run once via run_sandbox.py (imported automatically, see below) or
standalone: python scripts/seed_sandbox_intraday_data.py
"""
from __future__ import annotations

import datetime as dt
import os

import intraday_db as idb


def seed_if_empty() -> None:
    today = dt.date.today().isoformat()
    if idb.get_day(today) is not None:
        return  # already seeded (or the real engine already ran today)

    idb.record_day(today, 3.2, "LONG")
    idb.record_candidates(today, [
        {"rank": 1, "symbol": "HDFCBANK", "ret_first15_pct": 2.14},
        {"rank": 2, "symbol": "ICICIBANK", "ret_first15_pct": 1.87},
    ])
    idb.ensure_capital_seeded("paper", 1_000_000.0)

    # --- HDFCBANK: full lifecycle, closed with target + squareoff legs ---
    pos_id = idb.record_new_position(
        today, "HDFCBANK", "LONG", f"{today} 10:05:00",
        entry_price=1652.30, stop_price=1638.75, target_price=1679.40,
        qty=1476, mode="paper", signal_time=f"{today} 10:00:00")
    idb.close_position_leg(pos_id, "target", 738, 1679.40, f"{today} 11:20:00",
                          gross_pnl=20015.28, costs=612.40, net_pnl=19402.88)
    idb.close_position_leg(pos_id, "squareoff", 738, 1671.10, f"{today} 15:10:00",
                          gross_pnl=13875.60, costs=598.90, net_pnl=13276.70)

    # --- ICICIBANK: still watching for a breakout on an active signal ---
    idb.create_signal(today, "ICICIBANK", f"{today} 11:15:00",
                      signal_high=1247.80, signal_low=1239.20, signal_atr=9.65)

    day_pnl = 19402.88 + 13276.70
    idb.apply_day_pnl("paper", day_pnl)
    print(f"Seeded sandbox intraday demo data for {today}: "
         f"HDFCBANK closed (+Rs.{day_pnl:,.2f}), ICICIBANK signal active.")


if __name__ == "__main__":
    seed_if_empty()
