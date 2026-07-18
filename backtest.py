"""
Backtest engine for the NSE momentum + long-year-breakout strategy.

Design goals:
  * Reuses the EXACT production logic (indicators.compute_snapshot,
    screener.apply_gates, screener.score) — the backtest and the live
    screener cannot drift apart.
  * Point-in-time: on each rebalance date, every indicator is computed only
    from candles up to that date. No lookahead.
  * Realistic frictions: per-side costs (STT + charges + slippage) and stop
    fills at the stop price, not the close.

Known limitations (be honest with yourself about these):
  * Fundamental gates are DISABLED in the backtest — we cannot reconstruct
    what ROCE/D-E looked like on a past date from screener.in (survivorship/
    lookahead bias). The backtest therefore tests the technical engine only;
    live results with the quality gate on should differ (usually fewer, 
    higher-quality trades).
  * The universe itself is today's list — stocks that crashed out of the
    index are missing (survivorship bias). Treat absolute returns as
    optimistic; RELATIVE comparisons (with vs without breakout priority,
    parameter sensitivity) are what this tool is for.

Usage:
    python backtest.py --synthetic              # verify mechanics, no Kite
    python backtest.py --years 3                # real data via Kite (cached)
    python backtest.py --years 3 --no-breakout  # A/B: disable breakout tier
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import os

import numpy as np
import pandas as pd

import config
import indicators
import screener

CACHE_DIR = "cache"


# ---------------------------------------------------------------------------
# Data loading (Kite with on-disk daily cache, or synthetic)
# ---------------------------------------------------------------------------

def load_candles_cached(symbols: list[str], days: int) -> tuple[dict, pd.DataFrame]:
    """Fetch from Kite, caching each symbol as CSV (refreshed once per day).

    The cache-hit check only verifies the file was written today — it says
    nothing about whether the cached data's date range actually covers what
    THIS call asked for. A larger 'days' than what's cached needs a re-fetch;
    a smaller 'days' than what's cached (e.g. the user reruns the same day
    with a shorter lookback) needs the cached data trimmed down — otherwise
    a prior 3-year run's cache silently gets reused in full for a 1-year
    request. Both are handled by trimming to the requested window every time,
    regardless of whether the row above it was a cache hit or a fresh fetch.
    """
    import kite_client
    os.makedirs(CACHE_DIR, exist_ok=True)
    today = dt.date.today().isoformat()
    cutoff = pd.Timestamp(dt.date.today() - dt.timedelta(days=days))

    def _naive(frame: pd.DataFrame) -> pd.DataFrame:
        # Kite's timestamps are tz-aware (IST); cutoff above is naive.
        # Comparing tz-aware vs tz-naive raises TypeError, and which side
        # ends up tz-aware depends on whether a row came from a fresh Kite
        # fetch or a re-parsed CSV — normalizing to naive right after load
        # sidesteps that mismatch everywhere instead of patching every
        # comparison site individually.
        if not frame.empty and frame.index.tz is not None:
            frame = frame.copy()
            frame.index = frame.index.tz_localize(None)
        return frame

    out = {}
    for sym in symbols:
        path = os.path.join(CACHE_DIR, f"{sym}.csv")
        df = None
        if os.path.exists(path):
            cached = _naive(pd.read_csv(path, index_col=0, parse_dates=True))
            is_fresh = cached.attrs.get("stamp") == today or _stamp(path) == today
            covers_range = not cached.empty and cached.index.min() <= cutoff
            if is_fresh and covers_range:
                df = cached
        if df is None:
            df = _naive(kite_client.fetch_daily_candles(sym, days))
            if not df.empty:
                df.to_csv(path)
            import time; time.sleep(0.35)
        out[sym] = df[df.index >= cutoff] if not df.empty else df
    bpath = os.path.join(CACHE_DIR, "_NIFTY.csv")
    bench = None
    if os.path.exists(bpath) and _stamp(bpath) == today:
        cached_bench = _naive(pd.read_csv(bpath, index_col=0, parse_dates=True))
        if not cached_bench.empty and cached_bench.index.min() <= cutoff:
            bench = cached_bench
    if bench is None:
        bench = _naive(kite_client.benchmark_candles(days))
        bench.to_csv(bpath)
    bench = bench[bench.index >= cutoff]
    return out, bench


def _stamp(path: str) -> str:
    return dt.date.fromtimestamp(os.path.getmtime(path)).isoformat()


def make_synthetic_universe(n_symbols: int = 30, n_days: int = 900,
                            seed: int = 3) -> tuple[dict, pd.DataFrame]:
    """Synthetic market with momentum autocorrelation baked in, plus a few
    engineered long-base breakout stocks — used to verify engine mechanics."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=dt.date.today(), periods=n_days)

    def ohlcv(close):
        noise = 1 + np.abs(rng.normal(0, 0.006, len(close)))
        return pd.DataFrame({
            "open": close * (1 + rng.normal(0, 0.002, len(close))),
            "high": close * noise,
            "low": close / noise,
            "close": close,
            "volume": rng.integers(2e5, 9e5, len(close)).astype(float),
        }, index=dates)

    candles = {}
    for i in range(n_symbols):
        # persistent drift regime -> creates real momentum
        drift = rng.choice([-0.0008, 0.0002, 0.0012], p=[0.3, 0.4, 0.3])
        rets = rng.normal(drift, 0.016, n_days)
        # regime shift halfway for some names
        if rng.random() < 0.5:
            rets[n_days // 2:] += rng.choice([-0.001, 0.001])
        candles[f"SYM{i:02d}"] = ohlcv(100 * np.cumprod(1 + rets))

    # three engineered long-base breakout stocks
    for j in range(3):
        peak = 200
        c = np.concatenate([
            np.linspace(100, peak, 250),
            peak * (0.75 + 0.1 * np.sin(np.linspace(0, 9, n_days - 350)))
            + rng.normal(0, 1.5, n_days - 350),
            np.linspace(peak * 0.98, peak * 1.18, 100),
        ])
        candles[f"BRK{j}"] = ohlcv(c + rng.normal(0, 0.5, n_days))

    bench_rets = rng.normal(0.00035, 0.009, n_days)
    bench = ohlcv(100 * np.cumprod(1 + bench_rets))
    return candles, bench


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Position:
    symbol: str
    qty: int
    entry_price: float
    stop: float
    entry_date: pd.Timestamp
    priority: bool = False


@dataclasses.dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: int
    reason: str
    priority: bool

    @property
    def pnl(self):
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def ret_pct(self):
        return (self.exit_price / self.entry_price - 1) * 100

    @property
    def holding_days(self):
        return (self.exit_date - self.entry_date).days


def rank_universe_asof(candles: dict, bench: pd.DataFrame,
                       date: pd.Timestamp, cfg: dict) -> pd.DataFrame:
    """Point-in-time ranking: identical pipeline to the live screener, fed
    only data up to `date`. Fundamentals off (see module docstring)."""
    sliced = {s: df.loc[:date] for s, df in candles.items()
              if not df.empty and date in df.index}
    bench_slice = bench.loc[:date]
    tech = screener.build_technical_table(sliced, bench_slice)
    if tech.empty:
        return tech
    gated = screener.apply_gates(tech, fundamentals=None)
    return screener.score(gated, cfg)


def _pullback_trigger(df_upto: pd.DataFrame, cfg: dict) -> bool:
    """Only meaningful for symbols already on the gate-passing watchlist
    (trend/RSI/near-high checks are still done once a month at rebalance,
    same as the calendar mode). See indicators.pullback_trigger for the
    actual rule -- shared with live_rebalance.py so they can't drift apart."""
    return indicators.pullback_trigger(
        df_upto, cfg.get("pullback_ema_period", 20),
        cfg.get("pullback_tolerance_pct", 3.0))


def run_backtest(candles: dict, bench: pd.DataFrame,
                 cfg: dict | None = None,
                 initial_capital: float = 1_000_000,
                 cost_bps: float = 12.0,
                 rebalance: str = "MS",
                 warmup_days: int = 260,
                 verbose: bool = False) -> dict:
    """Monthly-rebalanced long-only backtest.

    Rules replayed exactly as the README workflow:
      entries : gate-passers (breakout priority first) fill any open slot
                the moment it's free -- at the rebalance itself, or on any
                later day a stop-loss frees one, rather than only at the
                next month's rebalance -- so capital doesn't sit idle in
                cash for weeks. entry_mode="ema_pullback" additionally
                requires a dip-to-EMA-and-bounce before buying; "calendar"
                buys the instant a slot is open. Both size equal-risk off
                the 2.5x ATR stop.
      stops   : if day's low touches the stop -> exit at stop (GTT proxy)
      exits   : at rebalance, drop anything below its 200 EMA or outside the
                top 2x max_positions ranking
    """
    cfg = dict(cfg or config.STRATEGY)
    cost = cost_bps / 10_000
    entry_mode = cfg.get("entry_mode", "calendar")

    dates = bench.index.sort_values()
    dates = dates[warmup_days:]
    # first trading day of each month
    rb_dates = set(pd.Series(dates).groupby(
        [dates.year, dates.month]).min())

    cash = initial_capital
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    curve = []
    # ema_pullback mode only: gate-passers awaiting a pullback trigger,
    # refreshed at each rebalance (same monthly cadence as everything else --
    # a stock's gate status can go stale for up to a month either way).
    watchlist: dict[str, pd.Series] = {}

    def close_position(sym, price, date, reason):
        nonlocal cash
        pos = positions.pop(sym)
        proceeds = pos.qty * price * (1 - cost)
        cash += proceeds
        trades.append(Trade(sym, pos.entry_date, date, pos.entry_price,
                            price * (1 - cost), pos.qty, reason, pos.priority))

    def try_enter(sym, row, price, stop, date):
        nonlocal cash
        if len(positions) >= cfg["max_positions"] or sym in positions:
            return
        equity_now = cash + sum(
            p.qty * float(candles[s].loc[date, "close"])
            for s, p in positions.items() if date in candles[s].index)
        qty = screener.position_size(equity_now, price, stop, cfg)
        qty = min(qty, int(cash / (price * (1 + cost))))
        if qty <= 0:
            return
        cash -= qty * price * (1 + cost)
        positions[sym] = Position(sym, qty, price * (1 + cost), stop, date,
                                  bool(row.get("priority", False)))
        if verbose:
            print(f"{date.date()} BUY  {sym:8s} x{qty} @ {price:.1f}"
                 f" stop {stop:.1f}{' 🚀' if positions[sym].priority else ''}"
                 f"{' (pullback)' if entry_mode == 'ema_pullback' else ''}")

    for date in dates:
        # 1) stop checks on today's bar
        for sym in list(positions):
            df = candles[sym]
            if date not in df.index:
                continue
            bar = df.loc[date]
            pos = positions[sym]
            if bar["low"] <= pos.stop:
                fill = min(pos.stop, bar["high"])  # gap-down fills lower
                fill = min(fill, bar["open"]) if bar["open"] < pos.stop else fill
                close_position(sym, fill, date, "stop")

        # 2) monthly rebalance: recompute the universe, drop trend/rank
        # failures, and refresh the standing watchlist (see step 2b).
        if date in rb_dates:
            ranked = rank_universe_asof(candles, bench, date, cfg)
            if not ranked.empty:
                candidates = ranked[ranked["all_gates"]]
                keep_zone = set(candidates.head(cfg["max_positions"] * 2).index)

                for sym in list(positions):
                    row = ranked.loc[sym] if sym in ranked.index else None
                    px = candles[sym].loc[date, "close"] if date in candles[sym].index else None
                    if px is None:
                        continue
                    if row is None or not bool(row["above_ema200"]) or sym not in keep_zone:
                        close_position(sym, float(px), date, "rebalance")

                # Replace the watchlist wholesale -- next rebalance is the
                # only re-evaluation of gates/ranking either way. Consumed
                # by step 2b below in BOTH entry modes, so a slot freed by a
                # stop mid-month doesn't sit in cash until next rebalance.
                watchlist = {sym: row for sym, row in candidates.iterrows()
                            if sym not in positions}

        # 2b) fill any open slot from the standing watchlist -- every day,
        # not just at rebalance, so freed-up capital gets redeployed right
        # away instead of idling in cash until next month. Calendar mode
        # buys the instant a slot opens; ema_pullback additionally requires
        # a dip-to-EMA-and-bounce before buying (same redeployment logic,
        # with an extra timing condition layered on top).
        if watchlist and len(positions) < cfg["max_positions"]:
            # Highest-score candidates get first pick of the limited slots.
            ordered = sorted(watchlist.items(),
                            key=lambda kv: kv[1].get("score", 0), reverse=True)
            for sym, row in ordered:
                if len(positions) >= cfg["max_positions"]:
                    break
                if sym in positions or date not in candles[sym].index:
                    continue
                df_upto = candles[sym].loc[:date]
                if entry_mode == "ema_pullback" and not _pullback_trigger(df_upto, cfg):
                    continue
                price = float(df_upto["close"].iloc[-1])
                atr_now = float(indicators.atr(df_upto, cfg["atr_period"]).iloc[-1])
                stop = price - cfg["atr_stop_multiple"] * atr_now
                try_enter(sym, row, price, stop, date)
                if sym in positions:
                    watchlist.pop(sym, None)

        # 3) mark to market
        mtm = cash + sum(
            p.qty * float(candles[s].loc[date, "close"])
            for s, p in positions.items() if date in candles[s].index)
        curve.append((date, mtm))

    # Positions still open when the date range runs out are left OPEN, not
    # force-liquidated — a forced "end" close was fictional (the position
    # never actually exited) and was contaminating win rate / avg-hold /
    # profit-factor stats with a same-day forced sale that wouldn't happen
    # in real trading. curve[] already carries each day's mark-to-market
    # value (cash + open positions at that day's close, see step 3 above),
    # so the equity curve's last point is already correct as-is — no
    # override needed once we stop liquidating.
    last = dates[-1]
    open_positions = []
    for sym, pos in positions.items():
        if candles[sym].loc[:last].empty:
            continue
        last_price = float(candles[sym].loc[:last, "close"].iloc[-1])
        unrealized_pnl = (last_price - pos.entry_price) * pos.qty
        open_positions.append({
            "symbol": sym, "entry_date": pos.entry_date, "entry_price": pos.entry_price,
            "current_price": last_price, "qty": pos.qty, "stop": pos.stop,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_ret_pct": (last_price / pos.entry_price - 1) * 100,
            "holding_days": (last - pos.entry_date).days,
            "priority": pos.priority,
        })
    open_positions_df = pd.DataFrame(open_positions)

    equity = pd.Series(dict(curve)).sort_index()
    metrics = compute_metrics(equity, trades, bench.loc[equity.index[0]:])
    metrics["Final Capital"] = round(float(equity.iloc[-1]), 2)
    metrics["Open positions"] = len(open_positions_df)
    return {
        "equity_curve": equity,
        "trades": pd.DataFrame([dataclasses.asdict(t) | {
            "pnl": t.pnl, "ret_pct": t.ret_pct, "holding_days": t.holding_days,
        } for t in trades]),
        "open_positions": open_positions_df,
        "final_capital": float(equity.iloc[-1]),
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(equity: pd.Series, trades: list[Trade],
                    bench: pd.DataFrame) -> dict:
    rets = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1
    dd = (equity / equity.cummax() - 1).min()
    sharpe = rets.mean() / rets.std() * np.sqrt(252) if rets.std() else np.nan

    b = bench["close"].reindex(equity.index).ffill()
    bench_cagr = (b.iloc[-1] / b.iloc[0]) ** (1 / years) - 1

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    prio = [t for t in trades if t.priority]
    non_prio = [t for t in trades if not t.priority]

    return {
        "CAGR %": round(cagr * 100, 2),
        "NIFTY CAGR %": round(bench_cagr * 100, 2),
        "Alpha (CAGR) %": round((cagr - bench_cagr) * 100, 2),
        "Sharpe": round(float(sharpe), 2),
        "Max drawdown %": round(dd * 100, 2),
        "Trades": len(trades),
        "Win rate %": round(100 * len(wins) / len(trades), 1) if trades else np.nan,
        "Profit factor": round(gross_win / gross_loss, 2) if gross_loss else np.inf,
        "Avg hold (days)": round(np.mean([t.holding_days for t in trades]), 0) if trades else np.nan,
        "Breakout trades": len(prio),
        "Breakout win rate %": round(100 * sum(t.pnl > 0 for t in prio) / len(prio), 1) if prio else np.nan,
        "Breakout avg ret %": round(np.mean([t.ret_pct for t in prio]), 2) if prio else np.nan,
        "Non-breakout trades": len(non_prio),
        "Non-breakout win rate %": round(100 * sum(t.pnl > 0 for t in non_prio) / len(non_prio), 1)
                                    if non_prio else np.nan,
        "Non-breakout avg ret %": round(np.mean([t.ret_pct for t in non_prio]), 2)
                                   if non_prio else np.nan,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true",
                    help="run on synthetic data (no Kite needed)")
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--cost-bps", type=float, default=12.0)
    ap.add_argument("--no-breakout", action="store_true",
                    help="A/B test: disable the long-year breakout bonus")
    ap.add_argument("--pullback-entry", action="store_true",
                    help="A/B test: enter on a pullback to the EMA instead "
                        "of immediately at the monthly rebalance close")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = dict(config.STRATEGY)
    if args.no_breakout:
        cfg["breakout_bonus"] = 0.0
    if args.pullback_entry:
        cfg["entry_mode"] = "ema_pullback"

    if args.synthetic:
        candles, bench = make_synthetic_universe()
    else:
        days = int(args.years * 365) + 400  # extra for indicator warmup
        candles, bench = load_candles_cached(config.UNIVERSE, days)

    res = run_backtest(candles, bench, cfg,
                       initial_capital=args.capital,
                       cost_bps=args.cost_bps, verbose=args.verbose)

    print("\n=== Metrics ===")
    for k, v in res["metrics"].items():
        print(f"{k:24s} {v}")

    res["equity_curve"].rename("equity").to_csv("backtest_equity.csv")
    res["trades"].to_csv("backtest_trades.csv", index=False)
    print("\nSaved: backtest_equity.csv, backtest_trades.csv")


if __name__ == "__main__":
    main()
