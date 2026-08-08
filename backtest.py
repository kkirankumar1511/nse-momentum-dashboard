"""
Backtest engine for the NSE calendar-entry momentum strategy.

Design goals:
  * Reuses the EXACT production logic (indicators.compute_snapshot,
    screener.apply_gates, screener.score) — the backtest and the live
    screener cannot drift apart.
  * Point-in-time: on each rebalance date, every indicator is computed only
    from candles up to that date. No lookahead.
  * Realistic frictions: per-side costs (STT + charges + slippage) and stop
    fills at the stop price, not the close.

Known limitations (be honest with yourself about these):
  * Fundamental gate is OFF by default and OPT-IN when enabled. Point-in-time
    scoring is real (not lookahead) -- run_backtest(fundamentals_history=...)
    uses xbrl_parser's known_as_of tagging (fundamentals_agent.build_
    fundamentals_history / score_asof) to only ever use filings that were
    actually public as of each rebalance date. Caveats that remain even with
    it on: PEG is unavailable (needs a live market price, which doesn't exist
    for a historical date); years reconstructed from summed quarters (see
    xbrl_parser.quarterly_summed_annual, needed once a symbol's history
    exceeds NSE's ~2-year primary-endpoint retention) can be missing some
    balance-sheet ratios, a pre-existing, documented limitation of that
    reconstruction, not something this feature introduces; and a same-day
    filing counts as "known" that day (~1 trading day of fuzziness, since
    filings land after market close).
  * The universe itself is today's list — stocks that crashed out of the
    index are missing (survivorship bias). Treat absolute returns as
    optimistic; RELATIVE comparisons (parameter sensitivity) are what this
    tool is for.

Usage:
    python backtest.py --synthetic  # verify mechanics, no Kite
    python backtest.py --years 3    # real data via Kite (cached)
    python backtest.py --years 5    # deep history (chunked Kite fetch)
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
import sector_universe

CACHE_DIR = "cache"
LONG_CACHE_DIR = os.path.join(CACHE_DIR, "long")


def _tz_naive(frame: pd.DataFrame) -> pd.DataFrame:
    """Kite's timestamps are tz-aware (IST); everything this module compares
    them against (cutoff dates, other cached frames) is tz-naive, so every
    fetch path needs this same normalization -- shared here rather than
    reimplemented per-function (load_candles_cached and
    load_long_history_cached both need it)."""
    if not frame.empty and frame.index.tz is not None:
        frame = frame.copy()
        frame.index = frame.index.tz_localize(None)
    return frame


# ---------------------------------------------------------------------------
# Data loading (Kite with on-disk daily cache, or synthetic)
# ---------------------------------------------------------------------------

def load_candles_cached(symbols: list[str], days: int,
                        end_date: dt.date | None = None,
                        progress_cb=None) -> tuple[dict, pd.DataFrame]:
    """Fetch from Kite, caching each symbol as CSV (refreshed once per day).

    progress_cb(stage: str, frac: float), if given, is called once per
    symbol -- lets a caller (e.g. dashboard.py's background-job wrapper)
    show real progress through this step instead of an indeterminate
    spinner. Reserves the last 0-5% of frac for the benchmark fetch below.

    The cache-hit check only verifies the file was written today — it says
    nothing about whether the cached data's date range actually covers what
    THIS call asked for. A larger 'days' than what's cached needs a re-fetch;
    a smaller 'days' than what's cached (e.g. the user reruns the same day
    with a shorter lookback) needs the cached data trimmed down — otherwise
    a prior 3-year run's cache silently gets reused in full for a 1-year
    request. Both are handled by trimming to the requested window every time,
    regardless of whether the row above it was a cache hit or a fresh fetch.

    end_date: if given (and before today), simulate a specific historical
    window instead of always running up to today. Kite is still fetched/
    cached up to real today as usual (so the on-disk cache is reusable
    across different end_date choices) -- this just trims the top off
    afterwards, same as the existing bottom trim by `days`.
    """
    import kite_client
    os.makedirs(CACHE_DIR, exist_ok=True)
    today = dt.date.today().isoformat()
    cutoff = pd.Timestamp(dt.date.today() - dt.timedelta(days=days))
    end_ts = pd.Timestamp(end_date) if end_date else None
    _naive = _tz_naive

    out = {}
    n = len(symbols)
    for i, sym in enumerate(symbols):
        if progress_cb:
            progress_cb(f"Fetching {sym} ({i + 1}/{n})...", (i + 1) / n * 0.95)
        path = os.path.join(CACHE_DIR, f"{sym}.csv")
        df = None
        if os.path.exists(path):
            cached = _naive(pd.read_csv(path, index_col=0, parse_dates=True))
            is_fresh = cached.attrs.get("stamp") == today or _stamp(path) == today
            covers_range = not cached.empty and cached.index.min() <= cutoff
            if is_fresh and covers_range:
                df = cached
        if df is None:
            import time
            fetched_fresh = False
            for attempt in range(3):
                try:
                    df = _naive(kite_client.fetch_daily_candles(sym, days))
                    fetched_fresh = True
                    break
                except Exception as e:
                    if attempt == 2:
                        # One flaky symbol shouldn't kill the whole batch --
                        # fall back to whatever's cached (even if stale/short)
                        # rather than crashing the entire multi-hour backtest
                        # data load over a single transient network blip.
                        print(f"[warn] {sym}: fetch failed after 3 attempts "
                             f"({e}); using stale cache if any, else empty")
                        df = cached if os.path.exists(path) else pd.DataFrame()
                    else:
                        time.sleep(2)
            if fetched_fresh and not df.empty:
                df.to_csv(path)
            time.sleep(0.35)
        sym_df = df[df.index >= cutoff] if not df.empty else df
        if end_ts is not None and not sym_df.empty:
            sym_df = sym_df[sym_df.index <= end_ts]
        out[sym] = sym_df
    if progress_cb:
        progress_cb("Fetching NIFTY benchmark...", 0.97)
    bpath = os.path.join(CACHE_DIR, "_NIFTY.csv")
    bench = None
    cached_bench = None
    if os.path.exists(bpath):
        cached_bench = _naive(pd.read_csv(bpath, index_col=0, parse_dates=True))
        is_fresh = _stamp(bpath) == today
        covers_range = not cached_bench.empty and cached_bench.index.min() <= cutoff
        if is_fresh and covers_range:
            bench = cached_bench
    if bench is None:
        # Same retry + stale-cache-fallback resilience the per-symbol loop
        # above already has -- this was previously a bare call with no
        # fallback, so a single flaky/expired-token moment killed the whole
        # multi-hour data load even though every symbol's own fetch already
        # tolerated exactly that.
        import time
        for attempt in range(3):
            try:
                bench = _naive(kite_client.benchmark_candles(days))
                bench.to_csv(bpath)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"[warn] NIFTY benchmark: fetch failed after 3 attempts "
                         f"({e}); using stale cache if any, else empty")
                    bench = cached_bench if cached_bench is not None else pd.DataFrame()
                else:
                    time.sleep(2)
    bench = bench[bench.index >= cutoff]
    if end_ts is not None and not bench.empty:
        bench = bench[bench.index <= end_ts]
    if progress_cb:
        progress_cb("Candles loaded", 1.0)
    return out, bench


def _stamp(path: str) -> str:
    return dt.date.fromtimestamp(os.path.getmtime(path)).isoformat()


def load_long_history_cached(symbols: list[str], min_days: int = 6100,
                             end_date: dt.date | None = None,
                             progress_cb=None) -> dict[str, pd.DataFrame]:
    """Deep daily history per symbol (~min_days=6100 -> ~16.7 years),
    cached separately from load_candles_cached()'s cache/{SYM}.csv --
    that cache is sized to whatever a single run's `days` window needs and
    gets FULLY re-fetched (all `days` worth) the moment it's a day stale,
    which is fine for a normal few-year backtest window but far too slow
    to do daily for 200+ symbols at 16-year depth.

    Only for the weekly/monthly confirmation gate (screener.apply_gates'
    weekly_monthly_gate_enabled), which needs deep history for its 200-bar
    weekly/monthly EMA lookbacks independent of whatever date range a
    given backtest run asked for -- see indicators._higher_tf_trend_ok.
    Every other backtest feature keeps using the run-scoped
    load_candles_cached(); this cache is never trimmed by a run's own
    `days`, so it only grows.

    end_date: the backtest run's own end date (None -> today, matching
    "Trailing years" mode which always runs through today). The cache is
    guaranteed fresh through at least this date -- explicit, not just
    incidental from always fetching to today -- and a custom `end_date`
    well in the past skips the fetch entirely once the cache already
    reaches it, even if real "today" has since moved on further.

    Once seeded (which does fetch through today -- Kite's API has no
    "as of a past date" fetch, and the extra rows are harmless, just
    sliced off by the caller's own point-in-time `.loc[:date]`), a stale
    cache fetches ONLY the days missing up to end_date and appends them
    (not a full min_days re-fetch) -- the whole point of a separate cache
    is to make this cheap on every subsequent day."""
    import time
    import kite_client
    os.makedirs(LONG_CACHE_DIR, exist_ok=True)
    target = end_date or dt.date.today()
    out = {}
    n = len(symbols)
    for i, sym in enumerate(symbols):
        if progress_cb:
            progress_cb(f"Long history {sym} ({i + 1}/{n})...", (i + 1) / n)
        path = os.path.join(LONG_CACHE_DIR, f"{sym}.csv")
        cached = None
        if os.path.exists(path):
            cached = _tz_naive(pd.read_csv(path, index_col=0, parse_dates=True))
        if cached is not None and not cached.empty:
            gap_days = (target - cached.index.max().date()).days
            if gap_days <= 0:
                out[sym] = cached
                continue
            try:
                # Kite always fetches through real today regardless of
                # `days`, so this naturally reaches target (target <=
                # today always, since a run's end_date can't be future).
                delta = _tz_naive(kite_client.fetch_daily_candles(sym, gap_days + 5))
                delta = delta[delta.index > cached.index.max()]
                combined = cached if delta.empty else pd.concat([cached, delta]).sort_index()
                combined.to_csv(path)
                out[sym] = combined
            except Exception as e:
                print(f"[warn] {sym}: long-history incremental fetch failed "
                     f"({e}); using stale cache ({gap_days}d behind)")
                out[sym] = cached
        else:
            try:
                full = _tz_naive(kite_client.fetch_daily_candles(sym, min_days))
                if not full.empty:
                    full.to_csv(path)
                out[sym] = full
            except Exception as e:
                print(f"[warn] {sym}: long-history seed fetch failed ({e}); skipping")
                out[sym] = pd.DataFrame()
        time.sleep(0.35)
    if progress_cb:
        progress_cb("Long history loaded", 1.0)
    return out


def make_synthetic_universe(n_symbols: int = 30, n_days: int = 900,
                            seed: int = 3) -> tuple[dict, pd.DataFrame]:
    """Synthetic market with momentum autocorrelation baked in, plus a few
    long-base-then-rally stocks for price-pattern diversity — used to verify
    engine mechanics without needing a live Kite connection."""
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

    # three extra long-base-then-rally stocks, for price-pattern diversity
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
    highest_close: float = 0.0


@dataclasses.dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: int
    reason: str

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
                       date: pd.Timestamp, cfg: dict,
                       fundamentals_history: dict | None = None,
                       score_cache: dict | None = None,
                       sector_candles: dict | None = None,
                       sector_membership: dict | None = None,
                       long_candles: dict | None = None,
                       precomputed: dict | None = None) -> pd.DataFrame:
    """Point-in-time ranking: identical pipeline to the live screener, fed
    only data up to `date`. Fundamental gate is off by default (fundamentals_
    history=None reproduces that exactly); pass a fundamentals_history dict
    (fundamentals_agent.build_fundamentals_history) to turn it on with a real
    point-in-time score (fundamentals_agent.score_asof), not lookahead.

    sector_candles/sector_membership: optional, from sector_universe.
    fetch_sector_index_candles()/get_sector_membership() -- both None
    (default) means no "sector_rs" column ever gets attached, so
    screener.score()'s sector-bonus guard never fires (byte-identical to
    before this feature existed).

    long_candles: optional, from load_long_history_cached() -- deep
    (~16-year) per-symbol history for the weekly/monthly confirmation
    gate's 200-bar EMA lookbacks, independent of however short `candles`
    is for this particular run. None (default) means weekly/monthly
    indicators fall back to whatever's in `candles` itself (see
    indicators.compute_snapshot), same as before this existed.

    precomputed: optional, {symbol: DataFrame} from run_backtest()'s
    one-time indicators.precompute_daily_series() call -- avoids
    recomputing ema/atr/rsi/macd from scratch inside `sliced` on every
    single call to this function (i.e. every rebalance day). None
    (default, and always the case for live callers today) means
    compute_snapshot recomputes them directly, exactly as before this
    param existed -- correct either way, this only changes speed."""
    sliced = {s: df.loc[:date] for s, df in candles.items()
              if not df.empty and date in df.index}
    bench_slice = bench.loc[:date]
    long_sliced = None
    if long_candles is not None:
        long_sliced = {s: df.loc[:date] for s, df in long_candles.items()
                       if not df.empty}
    precomputed_rows = None
    if precomputed is not None:
        precomputed_rows = {s: precomputed[s].loc[date] for s in sliced
                            if s in precomputed and date in precomputed[s].index}
    tech = screener.build_technical_table(sliced, bench_slice, cfg=cfg, long_candles=long_sliced,
                                          precomputed=precomputed_rows)
    if tech.empty:
        return tech
    fundamentals = None
    if fundamentals_history is not None:
        import fundamentals_agent
        fundamentals = fundamentals_agent.score_asof(fundamentals_history, date, score_cache)
    if sector_candles is not None and sector_membership is not None:
        sector_rank = sector_universe.sector_rs_asof(
            sector_candles, bench_slice, date, cfg["sector_rs_lookback_days"])
        tech["sector_rs"] = [
            sector_universe.stock_sector_rs(sym, sector_membership, sector_rank)
            for sym in tech.index]
    gated = screener.apply_gates(tech, fundamentals=fundamentals, cfg=cfg)
    return screener.score(gated, cfg)


def run_backtest(candles: dict, bench: pd.DataFrame,
                 cfg: dict | None = None,
                 initial_capital: float = 1_000_000,
                 cost_bps: float = 0.0,
                 rebalance: str = "MS",
                 warmup_days: int = 260,
                 verbose: bool = False,
                 fundamentals_history: dict | None = None,
                 sector_candles: dict | None = None,
                 sector_membership: dict | None = None,
                 long_candles: dict | None = None,
                 start_date: dt.date | None = None,
                 progress_cb=None) -> dict:
    """Monthly-rebalanced long-only backtest.

    start_date: clamps the actual simulated/traded date range to >= this
    date, on top of (not instead of) the warmup_days skip below -- the
    warmup skip alone only approximately lands near a caller's intended
    start (it depends on exactly how much candle history was fetched),
    so a caller asking for e.g. "2025-01-01 to 2025-06-30" could
    otherwise see real trades dated weeks before 2025-01-01. None
    (default) reproduces the original behavior exactly -- warmup_days
    alone decides where the simulation starts, as before this existed.

    cost_bps defaults to 0 -- Zerodha charges no brokerage on equity
    delivery (CNC). Statutory costs (STT, stamp duty, exchange/SEBI
    charges) still apply in reality (~5-7 bps round trip) and aren't
    broker-specific; pass a non-zero cost_bps (e.g. via --cost-bps on the
    CLI) to model them back in for a more conservative backtest.

    fundamentals_history: optional, from fundamentals_agent.build_
    fundamentals_history() -- turns on a real point-in-time fundamental
    quality gate (see module docstring's Known limitations). None (default)
    reproduces the original technical-only behavior exactly.

    sector_candles/sector_membership: optional, from sector_universe.
    fetch_sector_index_candles()/get_sector_membership() -- turns on the
    sector relative-strength score bonus (config.STRATEGY["sector_bonus_
    weight"], 0 by default). Both None (default) reproduces the original
    behavior exactly.

    long_candles: optional, from load_long_history_cached() -- deep
    (~16-year) per-symbol history feeding the weekly/monthly confirmation
    gate's 200-bar EMA lookbacks (config.STRATEGY["weekly_monthly_gate_
    enabled"], off by default), independent of however short `candles`
    is for this run. None (default) reproduces the original behavior --
    weekly/monthly indicators fall back to whatever's in `candles`, which
    is often too short for the 200-bar (or even 50-bar) lookback to ever
    resolve, silently failing that gate closed for the whole run.

    trailing stop: config.STRATEGY["trailing_stop_enabled"] (False by
    default) ratchets each position's stop up to highest_close_since_entry
    - trailing_atr_multiple*ATR as it gains, never back down -- see the
    daily loop's step 1b. Off by default reproduces the original fixed
    entry-stop behavior exactly.

    Rules replayed exactly as the README workflow:
      entries : gate-passers fill any open slot the moment it's free -- at
                the rebalance itself, or on any later day a stop-loss frees
                one, rather than only at the next month's rebalance -- so
                capital doesn't sit idle in cash for weeks. Sized equal-risk
                off the 2.5x ATR stop.
      stops   : if day's low touches the stop -> exit at stop (GTT proxy)
      exits   : at rebalance, drop anything below its 200 EMA or outside the
                top 2x max_positions ranking

    rebalance: "MS" (default) re-evaluates the 200-EMA/rank exit rule on the
    first trading day of each month only -- this is the cadence every
    documented A/B result in config.py/README was actually measured at.
    "D" re-evaluates it every trading day instead, matching the cadence
    live_rebalance.py's scheduled job actually runs at (Mon-Fri) if its
    proposal is executed that often -- added specifically to let that
    live/backtest cadence gap be measured rather than assumed. No other
    value is supported.
    """
    cfg = dict(cfg or config.STRATEGY)
    cost = cost_bps / 10_000
    # Created once per run (not module-level) so repeated backtests in one
    # process (Streamlit reruns, multiple CLI invocations) never share stale
    # state -- see fundamentals_agent.score_asof for the memoization key.
    score_cache: dict = {}

    # One-time, O(symbols x history) precompute of the causal daily EWM
    # indicators (ema/atr/rsi/macd) -- see indicators.precompute_daily_
    # series's docstring for why this is safe (not an approximation).
    # Replaces recomputing them from scratch inside a growing
    # df.loc[:date] slice on every single rebalance day below, which
    # profiled as ~87% of a daily-cadence backtest's total runtime.
    n_syms = len(candles)
    precomputed: dict = {}
    for i, (sym, df) in enumerate(candles.items()):
        if progress_cb and (i % max(1, n_syms // 20) == 0 or i == n_syms - 1):
            progress_cb(f"Precomputing indicators ({i + 1}/{n_syms})...",
                       (i + 1) / n_syms * 0.1)
        if not df.empty and len(df) >= cfg["ema_slow"]:
            precomputed[sym] = indicators.precompute_daily_series(df, cfg)

    dates = bench.index.sort_values()
    dates = dates[warmup_days:]
    if start_date is not None:
        dates = dates[dates >= pd.Timestamp(start_date)]
    if rebalance == "D":
        rb_dates = set(dates)
    else:
        # first trading day of each month
        rb_dates = set(pd.Series(dates).groupby(
            [dates.year, dates.month]).min())

    cash = initial_capital
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    curve = []
    # Gate-passers not yet held, refreshed at each rebalance (same monthly
    # cadence as everything else -- a stock's gate status can go stale for
    # up to a month either way) and consumed daily by step 2b so a slot
    # freed by a stop mid-month doesn't sit in cash until next rebalance.
    watchlist: dict[str, pd.Series] = {}
    # Full gate-passing set for the current month, INCLUDING held symbols --
    # watchlist deliberately excludes held ones (it's "what to buy next"),
    # but allocate_equal_weight_buys()'s top-up mechanic needs to know which
    # held positions are still legitimately good candidates this month, not
    # just which ones are newly biddable.
    current_candidate_syms: list[str] = []

    def close_position(sym, price, date, reason):
        nonlocal cash
        pos = positions.pop(sym)
        proceeds = pos.qty * price * (1 - cost)
        cash += proceeds
        trades.append(Trade(sym, pos.entry_date, date, pos.entry_price,
                            price * (1 - cost), pos.qty, reason))

    def _price_asof(sym: str, date) -> float | None:
        """Last known close at or before `date` -- forward-fills over a
        single-stock trading halt or an index-only session (confirmed via
        2017-10-19: NSE's Diwali Muhurat trading -- NIFTY has a normal
        candle that day, but only 4 of 208 universe stocks do). An exact
        `date` match instead of this silently priced every non-trading
        position at zero for that one day, producing a fictional
        flash-crash-to-near-zero point in the equity curve (traced a real
        -100% "max drawdown" to exactly this). None only if the symbol
        has no candle at all up to `date` (shouldn't happen for a
        position already held, since it must have had an entry-day
        candle at or before any later date)."""
        sliced = candles[sym].loc[:date, "close"]
        return float(sliced.iloc[-1]) if not sliced.empty else None

    def try_enter(sym, row, price, stop, date, qty_override=None):
        nonlocal cash
        if len(positions) >= cfg["max_positions"] or sym in positions:
            return
        if qty_override is not None:
            qty = qty_override
        else:
            equity_now = cash + sum(
                p.qty * (_price_asof(s, date) or 0.0)
                for s, p in positions.items())
            if cfg.get("capital_equal_weight_sizing", False):
                open_slots_remaining = cfg["max_positions"] - len(positions)
                qty = screener.capital_position_size(
                    equity_now, cash, price, open_slots_remaining, cfg["max_positions"])
            else:
                qty = screener.position_size(equity_now, price, stop, cfg)
        qty = min(qty, int(cash / (price * (1 + cost))))
        if qty <= 0:
            return
        cash -= qty * price * (1 + cost)
        entry_price = price * (1 + cost)
        positions[sym] = Position(sym, qty, entry_price, stop, date,
                                  highest_close=entry_price)
        if verbose:
            print(f"{date.date()} BUY  {sym:8s} x{qty} @ {price:.1f} stop {stop:.1f}")

    def top_up_position(sym, extra_qty, price, date):
        """EXPERIMENTAL, backtest-only -- adds to an ALREADY-open position
        (screener.allocate_equal_weight_buys' top-up mechanic), weighted-
        averaging the cost basis. Distinct from try_enter(), which only
        ever opens a brand-new position."""
        nonlocal cash
        if sym not in positions or extra_qty <= 0:
            return
        extra_qty = min(extra_qty, int(cash / (price * (1 + cost))))
        if extra_qty <= 0:
            return
        cost_amt = extra_qty * price * (1 + cost)
        pos = positions[sym]
        new_qty = pos.qty + extra_qty
        pos.entry_price = (pos.entry_price * pos.qty + price * (1 + cost) * extra_qty) / new_qty
        pos.qty = new_qty
        cash -= cost_amt
        if verbose:
            print(f"{date.date()} TOPUP {sym:8s} +{extra_qty} @ {price:.1f} (new qty {new_qty})")

    n_dates = len(dates)
    for i, date in enumerate(dates):
        # Throttled to ~100 updates over the whole run rather than every
        # single day -- calling into Streamlit's session state from here
        # on every trading day (thousands of them for a 5yr run) would add
        # measurable overhead for no visible benefit between updates that
        # close together anyway.
        if progress_cb and (i % max(1, n_dates // 100) == 0 or i == n_dates - 1):
            progress_cb(f"Simulating {date.date()}...", 0.1 + (i + 1) / n_dates * 0.9)
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

        # 1b) trailing stop: ratchet each surviving position's stop up to
        # highest_close_since_entry - trailing_atr_multiple*ATR, never back
        # down. Runs AFTER today's stop-check above, so today's own close
        # only affects tomorrow's check -- same causal, decide-off-
        # completed-bars model the rest of this engine already uses (see
        # try_enter's identical same-day ATR use for the entry stop).
        if cfg.get("trailing_stop_enabled", False):
            for sym, pos in positions.items():
                df = candles[sym]
                if date not in df.index:
                    continue
                pos.highest_close = max(pos.highest_close, float(df.loc[date, "close"]))
                atr_now = float(indicators.atr(df.loc[:date], cfg["atr_period"]).iloc[-1])
                new_stop = pos.highest_close - cfg["trailing_atr_multiple"] * atr_now
                if new_stop > pos.stop:
                    pos.stop = new_stop

        # 2) monthly rebalance: recompute the universe, drop trend/rank
        # failures, and refresh the standing watchlist (see step 2b).
        if date in rb_dates:
            ranked = rank_universe_asof(candles, bench, date, cfg,
                                       fundamentals_history, score_cache,
                                       sector_candles, sector_membership,
                                       long_candles, precomputed)
            if not ranked.empty:
                candidates = ranked[ranked["all_gates"]]
                keep_zone = set(candidates.head(cfg["max_positions"] * 2).index)

                for sym in list(positions):
                    px = candles[sym].loc[date, "close"] if date in candles[sym].index else None
                    if px is None:
                        continue
                    if screener.sell_check(sym, ranked, candidates, keep_zone,
                                          cfg["max_positions"], cfg):
                        close_position(sym, float(px), date, "rebalance")

                # Replace the watchlist wholesale -- next rebalance is the
                # only re-evaluation of gates/ranking either way. Consumed
                # by step 2b below in BOTH entry modes, so a slot freed by a
                # stop mid-month doesn't sit in cash until next rebalance.
                watchlist = {sym: row for sym, row in candidates.iterrows()
                            if sym not in positions}
                current_candidate_syms = list(candidates.index)

        # 2b) fill any open slot from the standing watchlist -- every day,
        # not just at rebalance, so freed-up capital gets redeployed right
        # away instead of idling in cash until next month.
        if watchlist and cfg.get("advanced_equal_weight_sizing", False):
            # EXPERIMENTAL: screener.allocate_equal_weight_buys() decides
            # the WHOLE day's allocation in one pass (equal-weight target,
            # cross-slot borrowing within tolerance, stop-not-substitute on
            # shortfall, top-up of underweighted existing holdings) instead
            # of try_enter()'s one-symbol-at-a-time greedy fill below.
            ordered_syms = [sym for sym, _ in sorted(
                watchlist.items(), key=lambda kv: kv[1].get("score", 0), reverse=True)
                if date in candles[sym].index]
            # Held positions that are STILL legitimately good candidates
            # this month (not being sold) -- eligible for the allocator's
            # top-up mechanic. Held symbols are never in `watchlist`
            # itself (that's specifically "what's biddable"), so without
            # this the top-up path could never fire at all.
            held_still_candidates = [sym for sym in current_candidate_syms
                                     if sym in positions and date in candles[sym].index]
            allocator_syms = ordered_syms + held_still_candidates
            prices = {sym: float(candles[sym].loc[:date, "close"].iloc[-1])
                     for sym in allocator_syms}
            held_info = {}
            for sym, pos in positions.items():
                p = _price_asof(sym, date)
                if p is not None:
                    held_info[sym] = (pos.qty, p)
                    prices.setdefault(sym, p)
            equity_now = cash + sum(
                p.qty * (_price_asof(s, date) or 0.0)
                for s, p in positions.items())
            alloc = screener.allocate_equal_weight_buys(
                allocator_syms, prices, held_info, cash_pool=cash,
                total_equity=equity_now, max_positions=cfg["max_positions"],
                tolerance_pct=cfg.get("equal_weight_tolerance_pct", 0.10))
            for sym, (qty, _reason) in alloc["new_buys"].items():
                if len(positions) >= cfg["max_positions"]:
                    break
                price = prices[sym]
                atr_now = float(indicators.atr(candles[sym].loc[:date], cfg["atr_period"]).iloc[-1])
                stop = price - cfg["atr_stop_multiple"] * atr_now
                try_enter(sym, watchlist[sym], price, stop, date, qty_override=qty)
                if sym in positions:
                    watchlist.pop(sym, None)
            for sym, extra_qty in alloc["top_ups"].items():
                top_up_position(sym, extra_qty, prices[sym], date)
        elif watchlist and len(positions) < cfg["max_positions"]:
            # Highest-score candidates get first pick of the limited slots.
            ordered = sorted(watchlist.items(),
                            key=lambda kv: kv[1].get("score", 0), reverse=True)
            for sym, row in ordered:
                if len(positions) >= cfg["max_positions"]:
                    break
                if sym in positions or date not in candles[sym].index:
                    continue
                df_upto = candles[sym].loc[:date]
                price = float(df_upto["close"].iloc[-1])
                atr_now = float(indicators.atr(df_upto, cfg["atr_period"]).iloc[-1])
                stop = price - cfg["atr_stop_multiple"] * atr_now
                try_enter(sym, row, price, stop, date)
                if sym in positions:
                    watchlist.pop(sym, None)

        # 3) mark to market
        mtm = cash + sum(
            p.qty * (_price_asof(s, date) or 0.0)
            for s, p in positions.items())
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
    }


def yearly_performance(equity: pd.Series, bench: pd.DataFrame,
                       trades: pd.DataFrame) -> pd.DataFrame:
    """Calendar-year breakdown of the equity curve vs NIFTY, plus each
    year's trade count/win rate (by exit date -- a trade's P&L is realized
    in the year it closes, not the year it opened). A year's starting value
    is the prior trading day's close if the equity curve extends before it
    (i.e. not the backtest's very first year), so a partial first/last
    calendar year is still a fair like-for-like return, not inflated by
    starting exactly at that year's first available price."""
    nifty = bench["close"].reindex(equity.index).ffill()
    rows = []
    for yr in sorted(equity.index.year.unique()):
        yr_eq = equity[equity.index.year == yr]
        prior_eq = equity[equity.index < yr_eq.index[0]]
        start_val = prior_eq.iloc[-1] if not prior_eq.empty else yr_eq.iloc[0]
        strat_ret = (yr_eq.iloc[-1] / start_val - 1) * 100

        yr_nifty = nifty[nifty.index.year == yr]
        prior_nifty = nifty[nifty.index < yr_nifty.index[0]]
        n_start = prior_nifty.iloc[-1] if not prior_nifty.empty else yr_nifty.iloc[0]
        nifty_ret = (yr_nifty.iloc[-1] / n_start - 1) * 100

        if not trades.empty:
            yr_trades = trades[pd.to_datetime(trades["exit_date"]).dt.year == yr]
        else:
            yr_trades = trades
        n_trades = len(yr_trades)
        win_rate = (100 * (yr_trades["pnl"] > 0).sum() / n_trades) if n_trades else np.nan

        rows.append({
            "Year": yr, "Strategy %": round(strat_ret, 2),
            "NIFTY %": round(nifty_ret, 2),
            "Alpha %": round(strat_ret - nifty_ret, 2),
            "Trades": n_trades,
            "Win rate %": round(win_rate, 1) if n_trades else np.nan,
        })
    return pd.DataFrame(rows).set_index("Year")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true",
                    help="run on synthetic data (no Kite needed)")
    ap.add_argument("--years", type=float, default=3.0,
                    help="trailing years from today (ignored if --start-date given)")
    ap.add_argument("--start-date", type=str, default=None,
                    help="YYYY-MM-DD -- simulate a specific historical window "
                        "instead of trailing --years from today")
    ap.add_argument("--end-date", type=str, default=None,
                    help="YYYY-MM-DD, defaults to today -- only used with --start-date")
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--cost-bps", type=float, default=0.0,
                    help="statutory costs (STT, stamp duty, exchange/SEBI "
                        "charges) per side -- 0 by default since Zerodha "
                        "charges no brokerage on equity delivery")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cfg = dict(config.STRATEGY)
    sim_start_date = None

    if args.synthetic:
        candles, bench = make_synthetic_universe()
    elif args.start_date:
        start = dt.datetime.strptime(args.start_date, "%Y-%m-%d").date()
        end = (dt.datetime.strptime(args.end_date, "%Y-%m-%d").date()
              if args.end_date else dt.date.today())
        days = (dt.date.today() - start).days + 400  # extra for indicator warmup
        candles, bench = load_candles_cached(config.UNIVERSE, days, end_date=end)
        sim_start_date = start
    else:
        days = int(args.years * 365) + 400  # extra for indicator warmup
        candles, bench = load_candles_cached(config.UNIVERSE, days)
        sim_start_date = dt.date.today() - dt.timedelta(days=int(args.years * 365))

    res = run_backtest(candles, bench, cfg,
                       initial_capital=args.capital,
                       cost_bps=args.cost_bps, verbose=args.verbose,
                       start_date=sim_start_date)

    print("\n=== Metrics ===")
    for k, v in res["metrics"].items():
        print(f"{k:24s} {v}")

    res["equity_curve"].rename("equity").to_csv("backtest_equity.csv")
    res["trades"].to_csv("backtest_trades.csv", index=False)
    print("\nSaved: backtest_equity.csv, backtest_trades.csv")


if __name__ == "__main__":
    main()
