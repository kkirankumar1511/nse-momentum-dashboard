"""
Smoke test for the new 13/21 EMA + VSA candlestick-pattern confluence
strategy (quant_pattern.py), per explicit request: "there is 2 rank - 1
is to select the stocks based on our current ranking and 2. to candle
pattern rank to make entry decision."

Rank 1 (stock selection): the SAME production watchlist pipeline used
throughout this session's "live rebalance" parity checks
(backtest.rank_universe_asof with the PDF-report-matching config --
RSI 45-75, EMA50/200, weekly/monthly gate ON, fundamental gate ON,
sector bonus 0.5) -- entirely unchanged. Rank1_top_n caps how many of
that day's gate-passers count as "the watchlist" (top 20, matching the
production Backtest UI's usual watchlist size).

Rank 2 (entry timing): quant_pattern.precompute_quant_pattern_signals,
computed independently per symbol over its full history, then only
CHECKED on days that symbol is in that day's Rank-1 watchlist.

Local-only, read-only. Prints, per day in the trailing --days window:
the Rank-1 watchlist, and any Rank-2 confirmed entries that day (score,
grade, pattern, entry/stop/target/rrr) -- a visibility/verification
run, not a full equity-curve backtest (8 days is too short for that).

Run with: python scripts/run_quant_pattern_backtest_local.py --days 8
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
import quant_pattern as qp

FUNDAMENTALS_HISTORY_CACHE = os.path.join("cache", "fundamentals_history.pkl")
SECTOR_DATA_CACHE = os.path.join("cache", "sector_data.pkl")

# Same "live rebalance" config used in this session's PDF-parity checks --
# this is Rank 1's stock-selection config, untouched by the new strategy.
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


def run_position_managed_backtest(scan_dates, long_candles, qp_signals,
                                  precomputed_daily, precomputed_weekly_monthly_ok,
                                  bench, fundamentals_history, sector_candles,
                                  sector_membership, rank1_top_n, max_positions,
                                  initial_capital=1_000_000):
    """Real portfolio simulation on top of the Rank-1/Rank-2 pipeline --
    explicit request ("execute this trade on top 20 only.. with max
    position open 5"): only `rank1_top_n` names are ever eligible per
    day, and at most `max_positions` can be held at once. Equal-weight
    sizing (equity/max_positions per slot), same convention used
    throughout this codebase's other backtests. Existing positions are
    checked for stop/target BEFORE new entries are considered each day,
    so an exit frees a slot the same day a new signal could fill it.

    2026-08-27: a plain fixed stop/target bracket -- three exit-
    management ideas were tried and reverted after each underperformed
    this simple version on the full 5-year backtest: a breakeven-ratchet
    trail, a 50%-exit-at-target + EMA13-runner for the rest, and a
    10-day time-stop (CAGR 14.80% -> 5.39% / 5.27% / 11.01%
    respectively). All three "fixed" a specific anecdotal problem
    (trades giving back large unrealized gains, or drifting for weeks
    before a slow stop-out) but each one converted more genuine
    eventual winners into early scratches/losses than it saved."""
    cash = initial_capital
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    equity_curve: list[tuple] = []

    for date in scan_dates:
        # 1) check existing positions for stop/target exit -- plain fixed
        # bracket, reverted 2026-08-27 after a breakeven trail, a 50%
        # partial-exit+EMA13-runner, AND a 10-day time-stop all
        # underperformed this simple version on the full 5-year backtest
        # (CAGR 14.80% -> 5.39%/5.27%/11.01% respectively).
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
                    "grade": pos["grade"], "pnl": pnl, "ret_pct": ret_pct,
                    "holding_days": (date - pos["entry_date"]).days,
                })
                del positions[sym]

        # 2) Rank-1 watchlist for today, capped at rank1_top_n
        ranked = bt.rank_universe_asof(
            long_candles, bench, date, RANK1_CFG,
            fundamentals_history, {}, sector_candles, sector_membership,
            long_candles, precomputed_daily, None, None, precomputed_weekly_monthly_ok)
        watchlist = []
        if not ranked.empty:
            passers = ranked[ranked["all_gates"]].sort_values("score", ascending=False)
            watchlist = list(passers.head(rank1_top_n).index)

        # 3) Rank-2 confirmed entries, only from the watchlist, only if a
        # slot is free -- ordered by pattern score descending, matching
        # this codebase's "highest-ranked fills first when slots are
        # scarce" convention.
        candidates = []
        for sym in watchlist:
            if sym in positions:
                continue
            sig = qp_signals.get(sym)
            if sig is None or date not in sig.index:
                continue
            row = sig.loc[date]
            if bool(row["confirmed_entry"]):
                candidates.append((sym, row))
        candidates.sort(key=lambda x: -x[1]["score"])

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
                "pattern": row["pattern_name"], "grade": row["grade"],
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
    ap.add_argument("--days", type=int, default=8,
                    help="how many trailing trading days to scan (default 8)")
    ap.add_argument("--rank1-top-n", type=int, default=20,
                    help="how many of each day's gate-passers count as the "
                        "Rank-1 watchlist (default 20)")
    ap.add_argument("--max-positions", type=int, default=None,
                    help="if given, runs a real position-managed backtest "
                        "(equal-weight sizing, capped concurrent positions) "
                        "instead of just listing every signal")
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--pattern-filter", type=str, default=None,
                    help="restrict entries to only this pattern_name, e.g. "
                        "'Bullish Engulfing' -- for isolated per-pattern A/B tests")
    ap.add_argument("--stop-mode", choices=["low_pct", "ema13_pct"], default=None,
                    help="overrides qp_stop_mode -- 'low_pct' (default) uses the "
                        "pattern candle's own low, 'ema13_pct' uses EMA13 instead, "
                        "both with the same qp_stop_buffer_pct below it")
    ap.add_argument("--confirm-on-close", action="store_true",
                    help="overrides qp_confirm_on_close -> True -- entry triggers "
                        "only when a later session's CLOSE reaches entry_level "
                        "(filling at that close), instead of an intraday high touch")
    ap.add_argument("--entry-expiry-sessions", type=int, default=None,
                    help="overrides qp_entry_expiry_sessions (default 1)")
    ap.add_argument("--entry-atr-buffer", type=float, default=None,
                    help="overrides qp_entry_atr_buffer (default 0.10); pass 0 "
                        "to trigger at the bare entry_level with no ATR cushion")
    args = ap.parse_args()

    qp_cfg = dict(qp.QUANT_PATTERN_DEFAULTS)
    if args.pattern_filter is not None:
        qp_cfg["qp_pattern_filter"] = args.pattern_filter
        print(f"Overriding qp_pattern_filter -> {args.pattern_filter!r} (from --pattern-filter).")
    if args.stop_mode is not None:
        qp_cfg["qp_stop_mode"] = args.stop_mode
        print(f"Overriding qp_stop_mode -> {args.stop_mode} (from --stop-mode).")
    if args.confirm_on_close:
        qp_cfg["qp_confirm_on_close"] = True
        print("Overriding qp_confirm_on_close -> True (from --confirm-on-close).")
    if args.entry_expiry_sessions is not None:
        qp_cfg["qp_entry_expiry_sessions"] = args.entry_expiry_sessions
        print(f"Overriding qp_entry_expiry_sessions -> {args.entry_expiry_sessions} (from --entry-expiry-sessions).")
    if args.entry_atr_buffer is not None:
        qp_cfg["qp_entry_atr_buffer"] = args.entry_atr_buffer
        print(f"Overriding qp_entry_atr_buffer -> {args.entry_atr_buffer} (from --entry-atr-buffer).")

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

    # Rank 2: precompute the pattern/signal state machine ONCE per symbol
    # over its full history (matches every other precompute_* convention
    # in this codebase -- point-in-time-safe, no lookahead, cheap to
    # re-slice per day afterward).
    print("Precomputing quant_pattern signals per symbol...")
    qp_signals: dict[str, pd.DataFrame] = {}
    for sym, df in long_candles.items():
        if df.empty or len(df) < max(qp_cfg["qp_ema_slow"], 60):
            continue
        qp_signals[sym] = qp.precompute_quant_pattern_signals(df, qp_cfg)

    # Rank 1's own precomputes -- same speedup this session already
    # validated for the production/paper-trading engines (avoids the slow
    # uncached-resample path for the weekly/monthly gate).
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
            scan_dates, long_candles, qp_signals, precomputed_daily,
            precomputed_weekly_monthly_ok, bench, fundamentals_history,
            sector_candles, sector_membership, args.rank1_top_n,
            args.max_positions, initial_capital=args.capital)

        print(f"\n=== Position-managed backtest: top {args.rank1_top_n} watchlist, "
             f"max {args.max_positions} concurrent positions ===")
        print(f"Closed trades: {len(trades)}, open at end: {open_at_end}")
        if not trades.empty:
            wins = (trades["ret_pct"] > 0).sum()
            print(f"Win rate: {wins/len(trades)*100:.1f}%")
            print(f"Avg return/trade: {trades['ret_pct'].mean():.2f}%")
            gross_win = trades.loc[trades["ret_pct"] > 0, "pnl"].sum()
            gross_loss = -trades.loc[trades["ret_pct"] < 0, "pnl"].sum()
            pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
            print(f"Profit factor: {pf:.2f}")
        final_equity = equity_curve["equity"].iloc[-1] if not equity_curve.empty else args.capital
        years = (equity_curve.index[-1] - equity_curve.index[0]).days / 365.25 if len(equity_curve) > 1 else 0
        if years > 0:
            cagr = (final_equity / args.capital) ** (1 / years) - 1
            print(f"CAGR (annualized from {years:.2f}y window): {cagr*100:.2f}%")
        print(f"Final equity: {final_equity:,.2f} (started {args.capital:,.0f})")

        out_dir = "result"
        os.makedirs(out_dir, exist_ok=True)
        trades.to_csv(os.path.join(out_dir, "quant_pattern_positionmanaged_trades.csv"), index=False)
        equity_curve.to_csv(os.path.join(out_dir, "quant_pattern_positionmanaged_equity.csv"))
        print(f"Saved: result/quant_pattern_positionmanaged_trades.csv, "
             f"result/quant_pattern_positionmanaged_equity.csv")
        return

    any_entries = []
    for date in scan_dates:
        ranked = bt.rank_universe_asof(
            long_candles, bench, date, RANK1_CFG,
            fundamentals_history, {}, sector_candles, sector_membership,
            long_candles, precomputed_daily, None, None, precomputed_weekly_monthly_ok)
        if ranked.empty:
            print(f"{date.date()}: no ranking data.")
            continue
        passers = ranked[ranked["all_gates"]].sort_values("score", ascending=False)
        watchlist = list(passers.head(args.rank1_top_n).index)
        print(f"=== {date.date()} -- Rank-1 watchlist ({len(watchlist)} of "
             f"{len(passers)} gate-passers) ===")
        print(", ".join(watchlist))

        hits = []
        for sym in watchlist:
            sig = qp_signals.get(sym)
            if sig is None or date not in sig.index:
                continue
            row = sig.loc[date]
            if bool(row["confirmed_entry"]):
                hits.append((sym, row))
        if hits:
            print(f"--- Rank-2 confirmed entries on {date.date()} ---")
            for sym, row in hits:
                print(f"  {sym:12s} pattern={row['pattern_name']:22s} "
                     f"grade={row['grade']:2s} score={row['score']:.0f} "
                     f"signal_date={pd.Timestamp(row['signal_date']).date()} "
                     f"entry={row['entry_price']:.2f} stop={row['stop']:.2f} "
                     f"target={row['target']:.2f} rrr={row['rrr']:.2f}")
                any_entries.append((date, sym, row))
        else:
            print("  (no Rank-2 confirmed entries today)")
        print()

    print(f"\n=== SUMMARY: {len(any_entries)} confirmed entries across "
         f"{len(scan_dates)} days ===")
    for date, sym, row in any_entries:
        print(f"{date.date()}  {sym:12s} {row['pattern_name']:22s} "
             f"grade={row['grade']} score={row['score']:.0f} "
             f"entry={row['entry_price']:.2f} stop={row['stop']:.2f} "
             f"target={row['target']:.2f} rrr={row['rrr']:.2f}")


if __name__ == "__main__":
    main()
