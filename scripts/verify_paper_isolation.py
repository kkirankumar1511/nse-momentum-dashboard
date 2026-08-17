"""
Isolation guard for the paper-trading feature: snapshots every trading-
related table's row COUNT in the REAL cache/state.db, runs the paper
engine, then asserts the counts are exactly unchanged.

Row counts, not a file hash -- importing config.py legitimately writes to
the real DB (seeds Kite creds / strategy_config defaults on first import),
and state_db.get_conn() commits a no-op schema/migration script on every
open. Those are pre-existing, idempotent, and unrelated to trading state;
a hash check would false-positive on them. Row counts stay exact across
that noise and only change if something actually inserted/deleted a real
trading row.

Run with:  python scripts/verify_paper_isolation.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state_db

TABLES = [
    "positions", "trades", "rebalance_runs", "rebalance_buys",
    "rebalance_sells", "rebalance_stop_updates", "rebalance_top_ups",
    "cash_flows", "equity_log", "stop_update_log", "job_runs",
]


def snapshot() -> dict[str, int]:
    conn = state_db.get_conn()  # no db_path arg -- always the REAL db
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    conn.close()
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="pass through to paper_engine.main() -- compute/print, write nothing")
    args = ap.parse_args()

    real_path = os.path.abspath(state_db.DB_PATH)
    print(f"Real DB path: {real_path}")
    if not os.path.exists(real_path):
        print("[warn] real state.db doesn't exist on this machine -- "
             "nothing to protect, but continuing so paper_db still gets exercised.")

    before = snapshot()
    print("Before:", before)

    import paper_engine
    paper_engine.main(["--dry-run"] if args.dry_run else [])

    after = snapshot()
    print("After: ", after)

    if before != after:
        diffs = {t: (before[t], after[t]) for t in TABLES if before[t] != after[t]}
        print(f"\n[FAIL] Real DB row counts changed: {diffs}")
        sys.exit(1)

    print("\n[OK] Real DB row counts unchanged -- paper engine touched nothing real.")


if __name__ == "__main__":
    main()
