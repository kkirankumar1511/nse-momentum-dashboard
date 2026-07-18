"""
Stage 1 of live automation: propose today's rebalance (sells + buys) by
running the exact same screener pipeline used live and in the backtest,
diffing it against your ACTUAL broker holdings.

This module NEVER places an order. It only computes a proposal and writes
it to disk for review -- you place orders yourself (Trade tab or broker
app). Meant to be run once a day, either from the dashboard's "Daily
Rebalance" tab or scheduled externally (Windows Task Scheduler / cron)
via `python live_rebalance.py`.

Why sells can lag a day: the rebalance rule (200 EMA / rank) is only ever
evaluated when this runs, so if you don't run it on a given day, a stock
that broke down that day won't be flagged until you next run it. Stops are
NOT covered here if you placed a GTT stop-loss at entry (kite_client.
place_gtt_stoploss) -- that already protects you intraday without needing
this job to run. This job only proposes the rebalance-rule exits/entries.
"""
from __future__ import annotations

import datetime as dt
import os

import pandas as pd

import config
import kite_client
import screener

PROPOSAL_CACHE = os.path.join("cache", "rebalance_proposal.pkl")


def get_live_holdings() -> pd.DataFrame:
    """Combined CNC holdings + any same-day positions, one row per symbol
    (qty, avg entry price), indexed by tradingsymbol. Delivery momentum
    swings mostly live in holdings; positions covers a same-day buy before
    it settles into holdings overnight."""
    frames = []
    pos = kite_client.get_positions()
    if not pos.empty and "quantity" in pos.columns:
        p = pos[pos["quantity"] != 0][["tradingsymbol", "quantity", "average_price"]]
        frames.append(p)
    hold = kite_client.get_holdings()
    if not hold.empty and "quantity" in hold.columns:
        h = hold[hold["quantity"] > 0][["tradingsymbol", "quantity", "average_price"]]
        frames.append(h)
    if not frames:
        return pd.DataFrame(columns=["quantity", "average_price"])
    combined = pd.concat(frames, ignore_index=True)
    combined["cost"] = combined["quantity"] * combined["average_price"]
    grouped = combined.groupby("tradingsymbol").agg(
        quantity=("quantity", "sum"), cost=("cost", "sum"))
    grouped["average_price"] = grouped["cost"] / grouped["quantity"]
    return grouped[["quantity", "average_price"]]


def propose_rebalance(available_cash: float, cfg: dict | None = None,
                      fundamentals: pd.DataFrame | None = None,
                      progress_cb=None) -> dict:
    """Returns {"run_time", "sells", "buys", "holdings", "open_slots"}.
    Nothing here executes an order -- see module docstring."""
    def report(stage, frac):
        if progress_cb:
            progress_cb(stage, frac)
    cfg = dict(cfg or config.STRATEGY)

    report("Loading current holdings...", 0.05)
    held = get_live_holdings()

    report("Scanning universe (screener pipeline)...", 0.10)
    ranked = screener.run_screen(
        with_fundamentals=True, fundamentals=fundamentals,
        progress_cb=lambda s, f: report(s, 0.10 + f * 0.7))

    candidates = ranked[ranked["all_gates"]]
    keep_zone = set(candidates.head(cfg["max_positions"] * 2).index)

    # ---- Sells: same rebalance rule as the backtest (200 EMA / rank) ----
    report("Checking held positions against the rebalance rule...", 0.85)
    sells = []
    for sym, row in held.iterrows():
        r = ranked.loc[sym] if sym in ranked.index else None
        if r is None:
            sells.append({"symbol": sym, "qty": int(row["quantity"]),
                         "avg_price": float(row["average_price"]),
                         "reason": "no data / not in current universe"})
        elif not bool(r.get("above_ema200", False)):
            sells.append({"symbol": sym, "qty": int(row["quantity"]),
                         "avg_price": float(row["average_price"]),
                         "reason": "closed below 200 EMA"})
        elif sym not in keep_zone:
            sells.append({"symbol": sym, "qty": int(row["quantity"]),
                         "avg_price": float(row["average_price"]),
                         "reason": f"dropped out of top {cfg['max_positions'] * 2} rank"})
    sells_df = pd.DataFrame(sells)

    # ---- Buys: fill slots opened up by the sells above ----
    report("Sizing new candidates...", 0.92)
    sold_syms = set(sells_df["symbol"]) if not sells_df.empty else set()
    still_held = set(held.index) - sold_syms
    open_slots = max(cfg["max_positions"] - len(still_held), 0)

    buys = []
    if open_slots > 0:
        for sym, row in candidates.iterrows():
            if len(buys) >= open_slots:
                break
            if sym in still_held:
                continue
            price = float(row["price"])
            stop = float(row["suggested_stop"])
            qty = screener.position_size(available_cash, price, stop, cfg)
            if qty <= 0:
                continue
            buys.append({
                "symbol": sym, "qty": qty, "price": round(price, 2),
                "stop": round(stop, 2), "score": float(row["score"]),
            })
    buys_df = pd.DataFrame(buys)

    report("Done", 1.0)
    result = {
        "run_time": dt.datetime.now(),
        "sells": sells_df,
        "buys": buys_df,
        "holdings": held.reset_index().rename(columns={"tradingsymbol": "symbol"}),
        "open_slots": open_slots,
    }
    os.makedirs("cache", exist_ok=True)
    pd.to_pickle(result, PROPOSAL_CACHE)
    return result


def main():
    try:
        margins = kite_client.get_margins()
        available_cash = margins["equity"]["available"]["live_balance"]
    except Exception as e:
        print(f"Kite connection failed (token may have expired): {e}")
        return

    def cb(stage, frac):
        print(f"[{frac * 100:5.1f}%] {stage}")

    result = propose_rebalance(available_cash, progress_cb=cb)

    print(f"\n=== Rebalance proposal ({result['run_time']:%d %b %Y %H:%M}) ===")
    print(f"Open slots: {result['open_slots']}")
    print("\n-- Proposed SELLS --")
    print(result["sells"].to_string(index=False) if not result["sells"].empty
         else "(none)")
    print("\n-- Proposed BUYS --")
    print(result["buys"].to_string(index=False) if not result["buys"].empty
         else "(none)")
    print(f"\nSaved: {PROPOSAL_CACHE}")
    print("Nothing was placed -- review and execute manually in the Trade tab.")


if __name__ == "__main__":
    main()
