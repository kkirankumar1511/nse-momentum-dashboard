"""
Multi-day BATCH backtest for the "DaysLowVolumnBreakout" intraday
strategy, over a short recent date range reachable via live Kite
historical data (kite_client.fetch_intraday_candles_range()) -- NOT a
reproduction of the v5.4 spec's own published 5-year numbers, which
come from E:\\trading-workspace\\intraday-pullback-trading's own local
disk cache of 5-min history; this dashboard has no access to that cache
and no cheap way to pull years of 5-minute candles for the whole F&O
universe from Kite's live API (100-day chunk limit per request, ~200
symbols). Intended for sanity-checking the CURRENTLY DEPLOYED rule set
(day bias -> candidate selection -> gap filter -> sector gate -> signal
detection -> CHRONO slot-filling -> position sizing -> exit management)
against real recent market data, the same way page_backtest() does for
the positional strategy.

Mirrors intraday_engine.py's run_live()/run_selection()/
resolve_sector_gates() decision logic as closely as a candle-granularity
batch replay can, but is a PURE, DB-free simulation: it never writes to
intraday_db's tables (those hold real paper/live trading history --
writing backtest rows there would corrupt it). Returns a plain dict of
DataFrames; the caller is responsible for caching/displaying the
result, exactly like backtest.py's own run_backtest() for the
positional strategy.

Candle data is cached to disk (cache/intraday_backtest/{5min,daily}/
{SYMBOL}.csv), mirroring backtest.py's own load_candles_cached() --
a past trading day's candles never change once that day has closed, so
re-fetching the whole F&O+NIFTY50 universe (~200+ symbols) from Kite on
every single run (including two runs over overlapping date ranges)
would pay the same slow, rate-limited cost for data already on disk.
Only the genuinely missing portion of a requested range is ever
fetched (extending the cache earlier, later, or both); the most recent
cached day is always treated as possibly incomplete and re-fetched, in
case today was still live when it was last written.
"""

from __future__ import annotations

import datetime as dt
import os
import time

import numpy as np
import pandas as pd

import config
import intraday_market as mkt
import intraday_strategy as strat
import kite_client
import nse_holidays
import sector_universe as su

EMA_WARMUP_DAYS = 120  # same depth as intraday_engine.py's EMA_WARMUP_DAYS
CACHE_DIR = os.path.join("cache", "intraday_backtest")

# v5.4 §0.15 -- rank convention for candidates surviving the gap filter.
# False = position in the PRE-filter top-N (1,3,4,5 when #2 gapped).
# True  = renumbered 1..N, which is what LIVE (intraday_engine.py) records.
# Measured over 5 years to decide which live should adopt -- see §0.15.
RENUMBER_RANK = True   # A/B run: live's convention




def _trading_days(start_date: dt.date, end_date: dt.date) -> list[dt.date]:
    days = []
    d = start_date
    while d <= end_date:
        if nse_holidays.is_trading_day(d):
            days.append(d)
        d += dt.timedelta(days=1)
    return days


def _candle_at(candles5: dict[str, pd.DataFrame], sym: str, day_ts: pd.Timestamp,
               hh: int, mm: int) -> pd.Series | None:
    """Was `df[df.index == ts]` -- a full boolean-equality SCAN of the
    symbol's entire multi-year 5-min history on every single call (this
    runs ~213 symbols x several checkpoint/candle lookups x ~1272 days
    over a 5-year backtest -- the dominant cost of the day-scanning
    phase). `.loc[ts]` resolves an exact timestamp via the DatetimeIndex's
    own hashtable instead -- same return value, O(1) average instead of
    O(n) per call."""
    df = candles5.get(sym)
    if df is None or df.empty:
        return None
    ts = day_ts + pd.Timedelta(hours=hh, minutes=mm)
    try:
        row = df.loc[ts]
    except KeyError:
        return None
    if isinstance(row, pd.DataFrame):  # duplicate timestamp -- old semantics took the first
        return row.iloc[0] if not row.empty else None
    return row


def _prev_close(daily: dict[str, pd.DataFrame], sym: str, day_ts: pd.Timestamp) -> float | None:
    """Was a full `.normalize()` + boolean-mask scan of the symbol's whole
    daily series on every call. `searchsorted` finds the same "latest
    close strictly before day_ts" via binary search instead (the daily
    index is already chronologically sorted, same assumption the old
    `.iloc[-1]`-on-a-filtered-series code relied on)."""
    d = daily.get(sym)
    if d is None or d.empty:
        return None
    idx = d.index if d.index.is_normalized else d.index.normalize()
    pos = idx.searchsorted(day_ts, side="left")
    return float(d["close"].iloc[pos - 1]) if pos > 0 else None


def _cache_path(kind: str, symbol: str) -> str:
    return os.path.join(CACHE_DIR, kind, f"{symbol}.csv")


def _read_cache(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    except Exception as e:
        print(f"[intraday_backtest] {path}: cache read failed, ignoring -- {e}")
        return pd.DataFrame()


def _write_cache(path: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path)


def _load_or_fetch_5min(symbol: str, warmup_from: dt.date, fetch_to: dt.date) -> pd.DataFrame:
    """5-min candles for `symbol` over [warmup_from, fetch_to], cached to
    disk across runs. Only fetches the piece(s) actually missing from
    the cache -- extending it earlier (if a new run's warmup reaches
    further back), later (if fetch_to moved forward), or both -- and
    always re-fetches from the cache's own last cached day onward
    whenever that day is today or later, since that day may have been
    cached while still mid-session and incomplete."""
    path = _cache_path("5min", symbol)
    cached = _read_cache(path)
    today = dt.date.today()

    if cached.empty:
        fresh = kite_client.fetch_intraday_candles_range(symbol, warmup_from, fetch_to)
        _write_cache(path, fresh)
        return fresh

    cached_min, cached_max = cached.index.min().date(), cached.index.max().date()
    pieces = [cached]

    if warmup_from < cached_min:
        back = kite_client.fetch_intraday_candles_range(
            symbol, warmup_from, cached_min - dt.timedelta(days=1))
        if not back.empty:
            pieces.append(back)

    if cached_max < fetch_to:
        fwd = kite_client.fetch_intraday_candles_range(
            symbol, cached_max + dt.timedelta(days=1), fetch_to)
        if not fwd.empty:
            pieces.append(fwd)
    elif cached_max >= today:
        # Last cached day is today (or later, if the clock rolled over
        # since) -- it may have been written mid-session, so refresh it
        # rather than trust a stale partial day.
        refresh = kite_client.fetch_intraday_candles_range(symbol, cached_max, max(fetch_to, today))
        if not refresh.empty:
            pieces.append(refresh)

    merged = pd.concat(pieces) if len(pieces) > 1 else cached
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    _write_cache(path, merged)
    return merged[(merged.index.normalize() >= pd.Timestamp(warmup_from))
                 & (merged.index.normalize() <= pd.Timestamp(fetch_to))]


def _load_or_fetch_daily(symbol: str, warmup_from: dt.date, fetch_to: dt.date) -> pd.DataFrame:
    """Daily candles for `symbol`, cached to disk. A cache whose range
    already fully covers [warmup_from, fetch_to] -- and whose last
    cached day is genuinely in the past, not today -- needs no fetch at
    all; otherwise re-fetches `days` back from today (cheap: daily
    candles have no 100-day chunk limit) and merges it in."""
    path = _cache_path("daily", symbol)
    cached = _read_cache(path)
    today = dt.date.today()

    if not cached.empty:
        cached_min, cached_max = cached.index.min().date(), cached.index.max().date()
        if cached_min <= warmup_from and cached_max >= fetch_to and cached_max < today:
            return cached

    days = (today - warmup_from).days + 5
    fresh = kite_client.fetch_daily_candles(symbol, days=days)
    if fresh.empty:
        return cached
    merged = pd.concat([cached, fresh]) if not cached.empty else fresh
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    _write_cache(path, merged)
    return merged


def _fetch_universe(symbols: list[str], candles5: dict, daily: dict,
                    warmup_from: dt.date, fetch_to: dt.date,
                    tick=None, stage: str = "Fetching candles") -> None:
    """Loads (from disk cache, fetching only what's missing) 5-min +
    daily candles for every symbol in `symbols` not already present in
    `candles5`, mutating both dicts in place. `tick`, if given, is
    called as tick(local_frac) -- a 0..1 fraction of THIS fetch loop's
    own progress -- once per symbol, both so a UI progress bar moves
    smoothly across a ~200-symbol fetch and so a background job's
    cooperative cancellation (raised from inside that callback) is
    checked often enough to actually feel responsive."""
    todo = [s for s in symbols if s not in candles5]
    failed: list[str] = []
    for i, sym in enumerate(todo):
        t0 = time.time()
        try:
            candles5[sym] = _load_or_fetch_5min(sym, warmup_from, fetch_to)
            daily[sym] = _load_or_fetch_daily(sym, warmup_from, fetch_to)
            # Kite's historical API is rate-limited to ~3 req/s (same
            # convention as kite_client.fetch_universe_candles's own
            # pause=0.35). _load_or_fetch_*'s own inter-CHUNK sleep only
            # fires within one symbol's multi-chunk fetch, so a run that
            # needs many never-before-cached symbols -- each resolved in
            # a single chunk -- fired them back-to-back with no pacing at
            # all. Measured 2026-10-22: a 168-symbol sector-constituent
            # backfill hit Kite's rate limit (surfaced as a generic
            # "Invalid api_key or access_token") around the 160th symbol,
            # reproduced twice. A pure cache hit takes milliseconds, so
            # only pace the iterations that actually went over the
            # network -- a fully-cached run stays exactly as fast as
            # before this fix.
            if time.time() - t0 > 0.05:
                time.sleep(0.35)
        except Exception as e:
            # v5.4 §0.11 -- an EXPIRED/INVALID Kite token is not a
            # per-symbol data gap, it is a dead run: every later symbol
            # fails the same way, the sector gate then fails those sectors
            # closed on every day, and the backtest still completes and
            # reports a number. Measured 2026-10-22: two runs finished
            # "successfully" at 349 and 1668 positions (vs 1627 on the
            # same config with a live token) purely because 68-69 symbols
            # came back empty on auth errors. Abort loudly instead.
            if "api_key" in str(e) or "access_token" in str(e) or "token" in str(e).lower():
                raise RuntimeError(
                    f"Kite auth failed while fetching {sym}: {e}. "
                    f"Refresh KITE_ACCESS_TOKEN and re-run -- continuing "
                    f"would silently produce a result computed from "
                    f"missing data.") from e
            print(f"[intraday_backtest] {sym}: candle fetch failed -- {e}")
            failed.append(sym)
            candles5[sym] = pd.DataFrame()
            daily[sym] = pd.DataFrame()
        if tick:
            tick((i + 1) / len(todo), f"{stage} ({i + 1}/{len(todo)})")

    # Even without an auth error, a large share of empty frames means the
    # run is not measuring the strategy -- the sector gate reads whatever
    # survived. 5% is well above normal (a genuine delisting or two) and
    # well below the 17% that produced the 349-position run.
    if todo and len(failed) > max(3, 0.05 * len(todo)):
        raise RuntimeError(
            f"{len(failed)}/{len(todo)} symbols returned no candles "
            f"({', '.join(failed[:8])}{'...' if len(failed) > 8 else ''}). "
            f"Refusing to report a backtest computed from this much "
            f"missing data.")


def run_backtest(start_date: dt.date, end_date: dt.date, capital: float,
                 progress_cb=None) -> dict:
    """Runs the full day-by-day simulation over [start_date, end_date]
    (inclusive), compounding `capital` across trading days. `progress_cb`,
    if given, is called as progress_cb(stage: str, frac: float) -- frac
    in [0, 1] over the WHOLE run -- the same (stage, frac) shape
    background_jobs.start_background_job() already wraps for every other
    long-running job in this dashboard (Screener, Live Rebalance, the
    positional Backtest page), so this can be dropped into that same
    machinery (progress bar + cooperative Stop button) unchanged."""
    def _progress(stage: str, frac: float) -> None:
        print(f"[intraday_backtest] ({frac:.0%}) {stage}")
        if progress_cb:
            progress_cb(stage, frac)

    trading_days = _trading_days(start_date, end_date)
    empty = {"trades": pd.DataFrame(), "daily": pd.DataFrame(), "candidates": pd.DataFrame(),
             "skipped_days": pd.DataFrame(), "start_date": start_date, "end_date": end_date,
             "capital_start": capital, "capital_end": capital}
    if not trading_days:
        return empty

    warmup_from = trading_days[0] - dt.timedelta(days=EMA_WARMUP_DAYS)
    fetch_to = trading_days[-1]

    # -- Phase A: universe candle fetch (NIFTY50 union F&O), ONE bulk
    # fetch per symbol over the whole [warmup_from, fetch_to] window --
    # never re-fetched per day (same perf discipline as intraday_engine.
    # py's own per-boundary refresh-window pattern, just at backtest scale).
    # Gets 0-70% of the overall bar -- by far the slowest phase (one Kite
    # historical-data call per symbol).
    _progress("Fetching NIFTY 50 + F&O universe constituents...", 0.0)
    nifty50 = mkt.fetch_nifty50_constituents()
    # v5.4 §0.10 -- UNIVERSE_RAW, not UNIVERSE. The skip list behind
    # config.UNIVERSE ("Skip stocks from scanner") is a POSITIONAL-trading
    # preference -- its own Admin help scopes it to "the Screener, Live
    # Rebalance, and backtest.py". Its current entries are all reason
    # "Higher Stock Value", i.e. too expensive to size a positional
    # holding in; that constraint does not apply to an intraday trade with
    # 5x leverage and a 0.50% risk budget. Reading UNIVERSE here silently
    # shrank the intraday universe 213 -> 207 (BOSCHLTD, FORCEMOT,
    # PAGEIND, POWERINDIA, SHREECEM, SOLARINDS).
    fno_syms = list(config.UNIVERSE_RAW)
    all_syms = sorted(set(nifty50) | set(fno_syms))

    candles5: dict[str, pd.DataFrame] = {}
    daily: dict[str, pd.DataFrame] = {}
    _fetch_stage = (f"Fetching {len(all_syms)} symbols' 5-min history "
                   f"({warmup_from} -> {fetch_to})")
    _progress(_fetch_stage, 0.02)
    _fetch_universe(all_syms, candles5, daily, warmup_from, fetch_to,
                    tick=lambda f, s: _progress(s, 0.02 + f * 0.68), stage=_fetch_stage)

    # -- Phase B: per-symbol continuous indicators, computed ONCE over
    # each symbol's whole multi-day series (Spec.md §1 -- never
    # cold-started per day).
    ema21_cache, atr14_cache, ema50_cache, ema10_cache = {}, {}, {}, {}
    for sym, df in candles5.items():
        if df is None or df.empty:
            continue
        ema21_cache[sym] = strat.ema21(df["close"])
        atr14_cache[sym] = strat.atr14(df)
        ema50_cache[sym] = strat.ema_n(df["close"], 50)
        ema10_cache[sym] = strat.ema_n(df["close"], strat.TRAIL_EMA_SLOW)

    # v5.4 §5s -- daily vol-regime floor input, from DAILY candles (not
    # 5-min), computed once per symbol over its whole series with NO
    # internal day-shift -- see daily_rng_atr()'s own docstring for why
    # the day-exclusion instead happens at LOOKUP time (_rng_atr_for()
    # below), via rng_atr_asof()'s strictly-before-day filter. This is
    # what makes a backtest day loop (which has every symbol's full
    # history, including "today", already loaded) and the live engine
    # (whose daily fetch naturally stops at yesterday) share one
    # identical lookup path instead of two separately-shifted ones.
    rng_atr_cache: dict[str, pd.Series] = {}
    for sym, dfd in daily.items():
        if dfd is None or dfd.empty:
            continue
        rng_atr_cache[sym] = strat.daily_rng_atr(dfd)

    def _rng_atr_for(sym: str, day: dt.date) -> float | None:
        return strat.rng_atr_asof(rng_atr_cache.get(sym), day)

    # -- Phase C: day-by-day day-bias + candidate selection + signal
    # detection. Sector gate NOT applied yet -- which sectors are even
    # touched isn't known until this phase finishes (same two-phase
    # shape as the reference backtest script).
    _progress("Running day-bias + candidate selection + signal detection...", 0.70)
    by_day: dict[dt.date, list[dict]] = {}
    skipped_days: list[dict] = []
    candidate_rows: list[dict] = []

    # Admin-editable operational knobs, read ONCE for the whole run (not
    # re-read per day) -- same keys/fallback convention intraday_engine.py
    # uses live, so a backtest and the live/paper engine always agree on
    # these even if Admin changes them mid-session. See config.py's own
    # comment by these keys for why only these five (not the strategy-edge
    # constants) are overridable.
    gap_filter_pct = config.STRATEGY.get("intraday_gap_filter_pct", strat.GAP_FILTER_PCT)
    top_n_candidates = config.STRATEGY.get("intraday_top_n_candidates", strat.TOP_N_CANDIDATES)
    max_trades_per_day = config.STRATEGY.get("intraday_max_trades_per_day", strat.MAX_TRADES_PER_DAY)
    leverage = config.STRATEGY.get("intraday_leverage", strat.LEVERAGE)
    risk_pct_per_trade = config.STRATEGY.get(
        "intraday_risk_pct_per_trade", strat.MAX_RISK_PCT_PER_TRADE * 100) / 100

    for day_i, day in enumerate(trading_days):
        _progress(f"Scanning {day}...", 0.70 + (day_i / len(trading_days)) * 0.15)
        day_ts = pd.Timestamp(day)

        def _closes_at(hh: int, mm: int) -> tuple[dict, dict]:
            close_map, prev_map = {}, {}
            for sym in all_syms:
                row = _candle_at(candles5, sym, day_ts, hh, mm)
                pc = _prev_close(daily, sym, day_ts)
                if row is not None:
                    close_map[sym] = float(row["close"])
                if pc is not None:
                    prev_map[sym] = pc
            return close_map, prev_map

        # v5.4 §0.12 -- day bias + the §5u rolling top-N union now come
        # from ONE shared implementation (strat.build_rolling_candidates),
        # the same one the research engine's list builder uses. This file
        # supplies DATA ACCESS only -- which candle to read, and how to
        # resolve a symbol's overnight gap -- never a decision. Matching
        # two hand-written copies of this logic by inspection failed three
        # times in one session (the bias boundary, the <50 F&O ranking
        # sample guard, and rank-before-vs-after the gap filter were each
        # found only after a 25-minute run disagreed).
        _cp_cache: dict[str, tuple[dict, dict]] = {}

        def _closes_at_cp(cp: str) -> tuple[dict, dict]:
            if cp not in _cp_cache:
                _h, _m = (int(x) for x in strat.checkpoint_candle(cp).split(":"))
                _cp_cache[cp] = _closes_at(_h, _m)
            return _cp_cache[cp]

        _gap_cache: dict[str, float | None] = {}

        def _gap_check(sym: str) -> float | None:
            # The 09:15 open vs prev close is a fixed DAY-level property,
            # independent of which checkpoint is doing the ranking --
            # resolved once per symbol per day, not once per checkpoint.
            if sym not in _gap_cache:
                c0915 = _candle_at(candles5, sym, day_ts, 9, 15)
                pc = _prev_close(daily, sym, day_ts)
                _gap_cache[sym] = ((float(c0915["open"]) - pc) / pc * 100.0
                                   if (c0915 is not None and pc) else None)
            return _gap_cache[sym]

        _built = strat.build_rolling_candidates(
            _closes_at_cp, nifty50, fno_syms,
            gap_check=_gap_check, gap_threshold=gap_filter_pct,
            top_n=top_n_candidates, renumber_rank=RENUMBER_RANK)
        bias = _built["bias"]
        checkpoint = _built["bias_checkpoint"]
        nifty_ratio = _built["nifty_ratio"]
        union = _built["candidates"]

        if bias is None:
            skipped_days.append({"date": day, "reason": "no_day_bias",
                                 "nifty_ratio": nifty_ratio})
            continue

        close_0925, prev_close_map = _closes_at_cp(checkpoint)

        if not union:
            skipped_days.append({"date": day, "reason": "no_candidates", "nifty_ratio": nifty_ratio})
            continue

        for sym, cand in sorted(union.items(), key=lambda kv: (kv[1]["entry_checkpoint"], kv[1]["rank"])):
            rank, ret, gap, entry_checkpoint = (cand["rank"], cand["ret"], cand["gap"],
                                                cand["entry_checkpoint"])
            window_start = strat.checkpoint_candle(entry_checkpoint)
            candidate_rows.append({"date": day, "symbol": sym, "rank": rank, "direction": bias,
                                  "ret_first15_pct": round(ret, 3), "gap_pct": gap,
                                  "nifty_ratio": round(nifty_ratio, 3), "checkpoint": checkpoint,
                                  "entry_checkpoint": entry_checkpoint})
            df5 = candles5.get(sym)
            if df5 is None or df5.empty or sym not in ema21_cache:
                continue
            dsub = df5[df5.index.normalize() == day_ts]
            if dsub.empty:
                continue
            fc = dsub.iloc[0]
            first_low, first_high = float(fc["low"]), float(fc["high"])
            c20 = dsub[dsub.index == day_ts + pd.Timedelta(hours=9, minutes=20)]
            c25 = dsub[dsub.index == day_ts + pd.Timedelta(hours=9, minutes=25)]
            if c20.empty or c25.empty:
                continue  # one-sided signal gate fails closed without both reference candles
            sig_range_low = min(float(c20.iloc[0]["low"]), float(c25.iloc[0]["low"]))
            sig_range_high = max(float(c20.iloc[0]["high"]), float(c25.iloc[0]["high"]))
            fvg_lo, fvg_hi = strat.fair_value_gap(first_high, first_low,
                                                  float(c25.iloc[0]["high"]), float(c25.iloc[0]["low"]))
            entry = strat.find_entry(dsub, bias, ema21_cache[sym], atr14_cache[sym],
                                     first_low, first_high, sig_range_low, sig_range_high,
                                     ema50_series=ema50_cache.get(sym), fvg_lo=fvg_lo, fvg_hi=fvg_hi,
                                     window_start=window_start)
            if entry is None:
                continue
            risk = abs(entry["entry_price"] - entry["stop_price"])
            by_day.setdefault(day, []).append({
                "symbol": sym, "direction": bias, "rank": rank, "dsub": dsub,
                "entry": entry, "risk": risk})

        # A day can clear day_bias and produce candidates (recorded in
        # candidate_rows above, visible in the "Daily candidates"
        # expander) yet still vanish from every OTHER output with no
        # explanation -- find_entry() returning None for every one of
        # them (no candle ever qualified as a signal, or none broke out)
        # is a perfectly normal, common outcome, not a bug, but leaving
        # it unrecorded here is exactly what made a legitimately quiet
        # stretch of days look like a mystery gap in the UI.
        if day not in by_day:
            skipped_days.append({"date": day, "reason": "no_entry_triggered", "nifty_ratio": nifty_ratio})

    n_pre_sector = sum(len(v) for v in by_day.values())
    _progress(f"{n_pre_sector} signal(s) across {len(by_day)} day(s) before the sector gate", 0.85)

    # -- Phase D: sector confirmation gate (v2 Spec §6) + sector-bias
    # annotation, resolved for EVERY ranked candidate (not just the ones
    # that went on to form a signal) -- so the "Daily candidates" table
    # can show each one's sector/sector-ratio/gate verdict, not just the
    # handful that actually traded. Fetches sector-constituent candle
    # data only for symbols not already covered by the universe fetch
    # above.
    all_cand_symbols = sorted({r["symbol"] for r in candidate_rows})
    sector_map: dict[str, str | None] = {}
    if all_cand_symbols:
        profiles = su.resolve_sector_profiles(all_cand_symbols)
        sector_map = {s: p.get("primary_sector") for s, p in profiles.items()}

    sectors_touched = {sector_map[s] for s in all_cand_symbols if sector_map.get(s)}
    catalog = su.fetch_index_constituents()
    sector_members = {sec: list(catalog.get(sec, {}).keys()) for sec in sectors_touched}
    all_sector_syms = sorted(set().union(*sector_members.values())) if sector_members else []
    new_sector_syms = [s for s in all_sector_syms if s not in candles5]
    if new_sector_syms:
        _sector_fetch_stage = (f"Fetching {len(new_sector_syms)} additional sector-constituent "
                               f"symbol(s) for the sector gate")
        _progress(_sector_fetch_stage, 0.86)
        _fetch_universe(new_sector_syms, candles5, daily, warmup_from, fetch_to,
                        tick=lambda f, s: _progress(s, 0.86 + f * 0.03), stage=_sector_fetch_stage)

    sector_ratio_cache: dict[tuple[str, dt.date], float | None] = {}

    # v5.4 spec copy 8 §0.3.4's three-part rule, part 3: a floor below
    # which a sector's ratio is never trusted however clean the resolved
    # subset looks -- NAUKRI's own incident (2026-10-09) scored a ratio
    # off 4 of 27 real constituents, which would satisfy "every member
    # that exists is resolved" on its own. The exact tolerance for part
    # 2 (how much slack a LARGE sector gets for one stale member) is
    # still an OPEN question in that spec -- not implemented here,
    # staying strict until it's actually measured rather than guessed.
    def _sector_ratio(sec: str, day: dt.date) -> float | None:
        """v5.4 spec copy 8 §0.3's three-part rule -- supersedes the old
        "fail closed on ANY missing constituent" rule (commit 33dee03),
        which was measured to cost ~50 CAGR points over 5 years on this
        exact ruleset because it's too strict for a BACKTEST specifically.
        `sector_members` is TODAY's index membership; applied unmodified
        to a historical date, most members simply hadn't listed yet
        (AEQUS: 2025-12-10, TMCV: 2025-11-12, ...) -- treating "not yet
        listed" the same as "data genuinely missing" rejected the sector
        (and the whole day, once every candidate's sector is rejected)
        for a reason that has nothing to do with data quality.
        intraday_engine.py's own resolve_sector_gates() stays fully
        strict unchanged -- LIVE has no not-yet-listed members (everything
        in today's index exists today), so strict is correct there; it
        only breaks replayed against the past.

        1. a constituent counts toward the denominator only if it existed
           by `day` -- a daily series with a bar on or before `day`. One
           with NO daily data at all is NOT assumed not-yet-listed (that's
           indistinguishable from a genuine fetch gap, e.g. the T2T/BE
           tradingsymbol-suffix gap kite_client.instrument_map() now
           covers, §0.3.6); only a non-empty series whose earliest bar is
           AFTER `day` proves it hadn't listed yet, and is excluded
           outright, not counted as a failure.
        2. fail closed (None) if ANY constituent that DID exist by `day`
           is still unresolved -- a real gap (stale cache, a suspension),
           exactly the NAUKRI failure mode.
        3. MIN_SECTOR_MEMBERS floor (see above) -- part 2 alone can't
           protect a thin sector; NAUKRI's 4-of-27 passed an "every
           resolved member agrees" check too, just among the wrong 4."""
        key = (sec, day)
        if key not in sector_ratio_cache:
            members = sector_members.get(sec, [])
            day_ts = pd.Timestamp(day)
            close_0925, prev_close_map, first_bar = {}, {}, {}
            for m in members:
                d = daily.get(m)
                if d is None or d.empty:
                    first_bar[m] = None  # fetch failed -- NOT not-yet-listed
                    continue
                fb = d.index.min().normalize().date()
                first_bar[m] = fb
                if fb > day:
                    continue  # proven not yet listed -- no price lookup needed
                row = _candle_at(candles5, m, day_ts, 9, 25)
                pc = _prev_close(daily, m, day_ts)
                if row is not None:
                    close_0925[m] = float(row["close"])
                if pc is not None:
                    prev_close_map[m] = pc
            sector_ratio_cache[key] = strat.sector_breadth_ratio(
                members, day, close_0925, prev_close_map, first_bar,
                policy=strat.SECTOR_POLICY_RESOLVED)
        return sector_ratio_cache[key]

    _progress("Resolving sector bias for every candidate...", 0.89)
    for row in candidate_rows:
        sec = sector_map.get(row["symbol"])
        if sec is None:
            row["sector"], row["sector_ratio"], row["sector_gate_pass"] = None, None, None
            continue
        ratio = _sector_ratio(sec, row["date"])
        row["sector"] = sec
        row["sector_ratio"] = round(ratio, 3) if ratio is not None else None
        row["sector_gate_pass"] = (strat.sector_gate_pass(ratio, row["direction"])
                                   if ratio is not None else False)

    gated_by_day: dict[dt.date, list[dict]] = {}
    n_dropped_no_sector = n_dropped_sector_fail = 0
    for day, cands in by_day.items():
        kept = []
        for c in cands:
            sec = sector_map.get(c["symbol"])
            if sec is None:
                n_dropped_no_sector += 1
                continue
            ratio = _sector_ratio(sec, day)  # already cached from the candidate_rows pass above
            if ratio is not None and strat.sector_gate_pass(ratio, c["direction"]):
                c["sector"] = sec
                c["sector_ratio"] = ratio
                kept.append(c)
            else:
                n_dropped_sector_fail += 1
        if kept:
            gated_by_day[day] = kept
        else:
            # Same visibility gap as Phase C's no_entry_triggered above --
            # this day DID produce a triggered signal, but every one of
            # them lost the sector-confirmation gate, so it would
            # otherwise disappear from daily/trades/skipped_days alike.
            skipped_days.append({"date": day, "reason": "all_sector_gate_failed",
                                "nifty_ratio": None})

    by_day = gated_by_day
    n_signals = sum(len(v) for v in by_day.values())
    _progress(f"{n_signals} signal(s) across {len(by_day)} day(s) after the sector gate "
             f"({n_dropped_no_sector} no primary sector, {n_dropped_sector_fail} sector ratio didn't confirm)",
             0.90)

    # v5.4 §5s -- daily vol-regime floor, resolved "beside the sector
    # gate" (same post-selection stage, per that spec's own §5s.9
    # placement note) -- NOT inside the earlier acceptance/ranking loop.
    # That distinction is load-bearing: the reference research engine's
    # own measured numbers come from a fixed, already-ranked top-N pool
    # with no deeper candidate to substitute in when one fails this gate
    # (so a drop there is effectively "mark and drop"); this backtest's
    # acceptance loop, by contrast, walks the FULL ranked universe and
    # WOULD genuinely substitute in a rank-6+ name if this gate were
    # applied there instead, inflating the effective pool size beyond
    # what was ever measured/validated.
    _progress("Resolving the daily vol-regime floor for every candidate...", 0.905)
    for row in candidate_rows:
        ra = _rng_atr_for(row["symbol"], row["date"])
        row["rng_atr"] = round(ra, 3) if ra is not None else None
        row["rng_atr_gate_pass"] = strat.rng_atr_gate_pass(ra)

    rngatr_gated_by_day: dict[dt.date, list[dict]] = {}
    n_dropped_rng_atr = 0
    for day, cands in by_day.items():
        kept = []
        for c in cands:
            ra = _rng_atr_for(c["symbol"], day)
            if strat.rng_atr_gate_pass(ra):
                c["rng_atr"] = ra
                kept.append(c)
            else:
                n_dropped_rng_atr += 1
        if kept:
            rngatr_gated_by_day[day] = kept
        else:
            # Same visibility reasoning as the sector gate's own empty-day
            # case just above -- this day cleared the sector gate but
            # every survivor was then too volatility-compressed to trade.
            skipped_days.append({"date": day, "reason": "all_rng_atr_gate_failed",
                                "nifty_ratio": None})

    by_day = rngatr_gated_by_day
    n_signals = sum(len(v) for v in by_day.values())
    _progress(f"{n_signals} signal(s) across {len(by_day)} day(s) after the vol-regime floor "
             f"({n_dropped_rng_atr} below MIN_RNG_ATR={strat.MIN_RNG_ATR})", 0.91)

    # -- Phase E: CHRONO slot-filling + capital-compounding day simulation.
    trades: list[dict] = []
    daily_rows: list[dict] = []
    capital_cur = capital
    _sim_days = sorted(by_day.keys())
    for sim_i, day in enumerate(_sim_days):
        _progress(f"Simulating {day}...", 0.90 + (sim_i / max(len(_sim_days), 1)) * 0.09)
        cands = by_day[day]
        # CHRONO (v5.4 §2.3/§5.1): take entries strictly in the order they
        # actually trigger, ties at the exact same timestamp broken by
        # rank -- the only selection rule a live system can genuinely
        # execute, matching run_live()'s own same-boundary tiebreak.
        ordered = sorted(cands, key=lambda c: (c["entry"]["entry_time"], c["rank"]))
        sigs = ordered[:max_trades_per_day]

        capital_alloc = (capital_cur / max_trades_per_day) * leverage
        risk_budget = capital_cur * risk_pct_per_trade
        day_pnl = 0.0
        capital_before = capital_cur

        for c in sigs:
            sym, direction, dsub, entry = c["symbol"], c["direction"], c["dsub"], c["entry"]
            qty = strat.position_size(capital_alloc, risk_budget, entry["entry_price"], entry["stop_price"])
            if qty <= 0:
                continue
            ema10_series = ema10_cache.get(sym, pd.Series(dtype=float))
            legs = strat.simulate_exit_v54(dsub, entry["entry_time"], entry["entry_price"],
                                           entry["stop_price"], direction, ema10_series)
            leg_qtys = strat.leg_quantities(qty, legs)
            side = 1 if direction == strat.LONG else -1
            position_id = f"{day}-{sym}-{entry['entry_time'].strftime('%H%M')}"
            n_legs = len(legs)
            for i, (leg, leg_qty) in enumerate(zip(legs, leg_qtys), start=1):
                gross = (leg["exit_price"] - entry["entry_price"]) * leg_qty * side
                cost = strat.round_trip_cost(entry["entry_price"], leg["exit_price"], leg_qty, direction)
                net = gross - cost
                day_pnl += net
                trades.append({
                    "position_id": position_id, "leg": f"{i}/{n_legs}", "date": day, "symbol": sym,
                    "direction": direction, "sector": c.get("sector"), "rank": c["rank"],
                    "signal_time": entry["signal_time"], "entry_time": entry["entry_time"],
                    "entry_price": round(entry["entry_price"], 2), "stop_price": round(entry["stop_price"], 2),
                    "qty": leg_qty, "exit_time": leg["exit_time"], "exit_price": round(leg["exit_price"], 2),
                    "reason": leg["reason"], "capital_open": round(capital_cur, 2),
                    "gross_pnl": round(gross, 2), "cost": round(cost, 2), "net_pnl": round(net, 2)})

        capital_cur += day_pnl
        daily_rows.append({"date": day, "day_pnl": round(day_pnl, 2), "capital": round(capital_cur, 2),
                           "return_pct": (day_pnl / capital_before * 100) if capital_before else 0.0,
                           "n_trades": len(sigs)})

    trades_df = pd.DataFrame(trades)
    daily_df = pd.DataFrame(daily_rows)
    candidates_df = pd.DataFrame(candidate_rows)
    n_positions = trades_df["position_id"].nunique() if not trades_df.empty else 0
    _progress(f"Done -- {n_positions} position(s), capital {capital:,.0f} -> {capital_cur:,.0f}", 1.0)

    return {
        "trades": trades_df, "daily": daily_df, "candidates": candidates_df,
        "skipped_days": pd.DataFrame(skipped_days), "start_date": start_date, "end_date": end_date,
        "capital_start": capital, "capital_end": capital_cur,
    }


# ---------------------------------------------------------------------------
# Post-run analytics -- trade-level R-multiples, monthly/risk-adjusted
# stats, drawdown episodes. Pure post-processing of run_backtest()'s own
# output; never re-simulates anything, so it can't itself introduce any
# new look-ahead risk.
# ---------------------------------------------------------------------------

def positions_from_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """Collapses `trades` (one row per LEG -- up to 2 per position, target
    half + runner half, each with its own partial qty/exit) to one row
    per POSITION -- the unit every trade-level stat below (win rate, R,
    profit factor) needs. qty is SUMMED across a position's legs (they
    were split FROM the original size by strat.leg_quantities(), so they
    sum back to it); entry_price/stop_price/direction/symbol/date are
    identical across a position's own legs, so just the first copy;
    net/gross_pnl/cost are summed (a position's TOTAL realized P&L, not
    one leg's own slice of it)."""
    if trades.empty:
        return pd.DataFrame(columns=["position_id", "date", "symbol", "direction", "rank", "sector",
                                     "entry_time", "exit_time", "entry_price", "stop_price", "qty",
                                     "net_pnl", "gross_pnl", "cost", "R"])
    g = trades.groupby("position_id", sort=False)
    pos = g.agg(
        date=("date", "first"), symbol=("symbol", "first"), direction=("direction", "first"),
        rank=("rank", "first"), sector=("sector", "first"),
        entry_time=("entry_time", "first"), exit_time=("exit_time", "max"),
        entry_price=("entry_price", "first"), stop_price=("stop_price", "first"),
        qty=("qty", "sum"), net_pnl=("net_pnl", "sum"), gross_pnl=("gross_pnl", "sum"),
        cost=("cost", "sum"),
    ).reset_index()
    risk_per_share = (pos["entry_price"] - pos["stop_price"]).abs()
    risk_total = (risk_per_share * pos["qty"]).replace(0, np.nan)
    pos["R"] = pos["net_pnl"] / risk_total
    return pos


def trade_level_metrics(pos: pd.DataFrame) -> dict:
    """positions/win-rate/R-multiple stats. `pos`: positions_from_trades()'
    own output (one row per position, with an "R" column already)."""
    if pos.empty:
        return {"positions": 0, "win_rate_pct": np.nan, "mean_R": np.nan, "median_R": np.nan,
                "max_R": np.nan, "p_r_ge_2_pct": np.nan, "avg_win_R": np.nan, "avg_loss_R": np.nan,
                "payoff_ratio": np.nan, "profit_factor": np.nan, "cost_pct_of_gross_profit": np.nan}
    R = pos["R"].dropna()
    wins_R, losses_R = R[R > 0], R[R <= 0]
    gross_profit = pos.loc[pos["gross_pnl"] > 0, "gross_pnl"].sum()
    gross_loss = -pos.loc[pos["gross_pnl"] <= 0, "gross_pnl"].sum()
    net_gross_win = pos.loc[pos["net_pnl"] > 0, "net_pnl"].sum()
    net_gross_loss = -pos.loc[pos["net_pnl"] <= 0, "net_pnl"].sum()
    return {
        "positions": int(len(pos)),
        "win_rate_pct": float((pos["net_pnl"] > 0).mean() * 100),
        "mean_R": float(R.mean()) if not R.empty else np.nan,
        "median_R": float(R.median()) if not R.empty else np.nan,
        "max_R": float(R.max()) if not R.empty else np.nan,
        "p_r_ge_2_pct": float((R >= 2).mean() * 100) if not R.empty else np.nan,
        "avg_win_R": float(wins_R.mean()) if not wins_R.empty else np.nan,
        "avg_loss_R": float(losses_R.mean()) if not losses_R.empty else np.nan,
        "payoff_ratio": (abs(wins_R.mean() / losses_R.mean())
                        if not wins_R.empty and not losses_R.empty and losses_R.mean() != 0 else np.nan),
        # Net profit factor -- AFTER costs, matching what the strategy
        # actually keeps (gross-only would overstate the edge).
        "profit_factor": (net_gross_win / net_gross_loss) if net_gross_loss else np.inf,
        # Costs against gross WINNING P&L specifically (how much of the
        # gross upside costs eat into), not against net or total gross --
        # the standard "cost drag on profit" framing.
        "cost_pct_of_gross_profit": float(pos["cost"].sum() / gross_profit * 100) if gross_profit else np.nan,
    }


def r_distribution_buckets(pos: pd.DataFrame) -> pd.DataFrame:
    """≤-1 / -1..0 / 0..1 / 1..2 / 2..3 / 3..5 / ≥5, right-closed (so
    exactly R=-1.00 -- the modal stop-out -- lands in "≤-1", not "-1..0")."""
    labels = ["≤-1", "-1..0", "0..1", "1..2", "2..3", "3..5", "≥5"]
    if pos.empty or pos["R"].dropna().empty:
        return pd.DataFrame({"bucket": labels, "count": [0] * len(labels), "pct": [0.0] * len(labels)})
    R = pos["R"].dropna()
    bins = [-np.inf, -1, 0, 1, 2, 3, 5, np.inf]
    cut = pd.cut(R, bins=bins, labels=labels, right=True)
    counts = cut.value_counts().reindex(labels, fill_value=0)
    return pd.DataFrame({"bucket": labels, "count": counts.values,
                        "pct": (counts.values / len(R) * 100).round(1)})


def monthly_table(daily: pd.DataFrame, capital_start: float) -> pd.DataFrame:
    """One row per calendar month that had at least one trading day.
    monthly_pct is computed against EQUITY AT THE START of that month
    (the prior month's closing capital, or the backtest's own starting
    capital for its first month) -- never against cumulative/compounded
    totals, which would understate a bad month that followed a good one
    and overstate one that followed a drawdown."""
    if daily.empty:
        return pd.DataFrame(columns=["month", "month_pnl", "equity_start", "equity_end",
                                     "n_trades", "monthly_pct"])
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").reset_index(drop=True)
    # capital_before: equity at the START of each trading day -- the
    # first trading day of a month's own capital_before IS that month's
    # starting equity (today's close minus today's own P&L).
    d["capital_before"] = d["capital"] - d["day_pnl"]
    d.loc[0, "capital_before"] = capital_start  # the very first day starts from the run's own seed
    d["month"] = d["date"].dt.to_period("M")
    monthly = d.groupby("month", sort=True).agg(
        month_pnl=("day_pnl", "sum"), equity_start=("capital_before", "first"),
        equity_end=("capital", "last"), n_trades=("n_trades", "sum"),
    ).reset_index()
    monthly["monthly_pct"] = monthly["month_pnl"] / monthly["equity_start"].replace(0, np.nan) * 100
    return monthly


def monthly_metrics(monthly: pd.DataFrame) -> dict:
    if monthly.empty:
        return {"negative_months": 0, "total_months": 0, "months_lt_neg5_pct": 0,
                "months_ge_2_pct": 0, "mean_monthly_pct": np.nan, "median_monthly_pct": np.nan,
                "best_month_pct": np.nan, "worst_month_pct": np.nan,
                "downside_deviation": np.nan, "monthly_win_rate_pct": np.nan}
    pct = monthly["monthly_pct"].dropna()
    return {
        "negative_months": int((pct < 0).sum()), "total_months": int(len(pct)),
        "months_lt_neg5_pct": int((pct < -5).sum()), "months_ge_2_pct": int((pct >= 2).sum()),
        "mean_monthly_pct": float(pct.mean()), "median_monthly_pct": float(pct.median()),
        "best_month_pct": float(pct.max()), "worst_month_pct": float(pct.min()),
        # sqrt(mean(min(r,0)^2)) -- only losing months count against this,
        # a winning month contributes exactly 0, never a negative penalty.
        "downside_deviation": float(np.sqrt((pct.clip(upper=0) ** 2).mean())),
        "monthly_win_rate_pct": float((pct > 0).mean() * 100),
    }


def monthly_buckets(monthly: pd.DataFrame) -> pd.DataFrame:
    """<0% / 0-1% / 1-2% / 2-5% / ≥5%, left-closed (so exactly 0% lands
    in "0-1%", exactly 5% lands in "≥5%") -- the "empty middle" this is
    meant to expose is whether most months cluster at the extremes
    (<0% and ≥5%) rather than the merely-positive 0-2% band."""
    labels = ["<0%", "0-1%", "1-2%", "2-5%", "≥5%"]
    if monthly.empty or monthly["monthly_pct"].dropna().empty:
        return pd.DataFrame({"bucket": labels, "count": [0] * len(labels)})
    pct = monthly["monthly_pct"].dropna()
    bins = [-np.inf, 0, 1, 2, 5, np.inf]
    cut = pd.cut(pct, bins=bins, labels=labels, right=False)
    counts = cut.value_counts().reindex(labels, fill_value=0)
    return pd.DataFrame({"bucket": labels, "count": counts.values})


def _equity_curve_with_seed(daily: pd.DataFrame, capital_start: float) -> tuple[pd.Series, pd.Series]:
    """(dates, equity) with the run's own starting capital prepended as
    day zero -- every drawdown/CAGR calc below needs a true starting
    reference point, not just the first TRADED day's own closing value
    (which could itself already be under water if day 1 lost money)."""
    if daily.empty:
        return pd.Series([pd.NaT]), pd.Series([capital_start])
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").reset_index(drop=True)
    dates = pd.concat([pd.Series([d["date"].iloc[0]]), d["date"]], ignore_index=True)
    equity = pd.concat([pd.Series([float(capital_start)]), d["capital"]], ignore_index=True)
    return dates, equity


def risk_adjusted_metrics(monthly: pd.DataFrame, daily: pd.DataFrame, capital_start: float,
                          start_date: dt.date, end_date: dt.date) -> dict:
    mm = monthly_metrics(monthly)
    mean_m, down_dev = mm["mean_monthly_pct"], mm["downside_deviation"]
    sortino = (mean_m / down_dev * np.sqrt(12)) if down_dev else np.nan

    _, equity = _equity_curve_with_seed(daily, capital_start)
    max_dd = float((equity / equity.cummax() - 1).min() * 100) if len(equity) > 1 else 0.0

    final_equity = float(equity.iloc[-1])
    years = max((end_date - start_date).days / 365.25, 1e-9)
    cagr = ((final_equity / capital_start) ** (1 / years) - 1) * 100 if capital_start else np.nan
    calmar = (cagr / abs(max_dd)) if max_dd else np.nan

    # Month-end drawdown: same cummax/drawdown math, but sampled only at
    # each month's OWN closing equity -- smooths out intra-month noise,
    # answering "how bad did it look if you only checked at month-end."
    if not monthly.empty:
        month_end_eq = pd.concat([pd.Series([float(capital_start)]), monthly["equity_end"]], ignore_index=True)
        month_end_dd = float((month_end_eq / month_end_eq.cummax() - 1).min() * 100)
    else:
        month_end_dd = 0.0

    return {"sortino": sortino, "calmar": calmar, "max_drawdown_pct": max_dd,
           "month_end_drawdown_pct": month_end_dd, "cagr_pct": cagr, "final_equity": final_equity,
           "total_return_pct": (final_equity / capital_start - 1) * 100 if capital_start else np.nan}


def drawdown_episodes(daily: pd.DataFrame, capital_start: float, top_n: int = 5) -> pd.DataFrame:
    """Every peak-to-recovery episode (a NEW all-time-high partitions the
    equity curve into episodes), ranked by TOTAL calendar days (peak to
    recovery) -- duration matters as much as depth for whether a real
    account could be held through it. The most recent episode, if the
    curve hasn't made a new high since, is reported as "ongoing"
    (recovery_date/trough_to_recovery_days are None) rather than
    silently dropped or treated as resolved."""
    cols = ["peak_date", "trough_date", "recovery_date", "peak_to_trough_days",
           "trough_to_recovery_days", "total_days", "depth_pct", "ongoing"]
    dates, equity = _equity_curve_with_seed(daily, capital_start)
    if len(equity) < 2:
        return pd.DataFrame(columns=cols)
    cummax = equity.cummax()
    peak_idx = equity.index[equity == cummax].tolist()

    episodes = []
    for i in range(len(peak_idx) - 1):
        s, e = peak_idx[i], peak_idx[i + 1]
        if e <= s:
            continue
        seg = equity.iloc[s:e + 1]
        t_idx = seg.idxmin()
        peak_d, trough_d, rec_d = dates.iloc[s], dates.iloc[t_idx], dates.iloc[e]
        episodes.append({
            "peak_date": peak_d, "trough_date": trough_d, "recovery_date": rec_d,
            "peak_to_trough_days": (trough_d - peak_d).days,
            "trough_to_recovery_days": (rec_d - trough_d).days,
            "total_days": (rec_d - peak_d).days, "depth_pct": (seg.min() / equity.iloc[s] - 1) * 100,
            "ongoing": False,
        })
    if peak_idx and peak_idx[-1] < len(equity) - 1:
        s = peak_idx[-1]
        seg = equity.iloc[s:]
        t_idx = seg.idxmin()
        peak_d, trough_d, last_d = dates.iloc[s], dates.iloc[t_idx], dates.iloc[-1]
        episodes.append({
            "peak_date": peak_d, "trough_date": trough_d, "recovery_date": None,
            "peak_to_trough_days": (trough_d - peak_d).days, "trough_to_recovery_days": None,
            "total_days": (last_d - peak_d).days, "depth_pct": (seg.min() / equity.iloc[s] - 1) * 100,
            "ongoing": True,
        })
    if not episodes:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(episodes)
    # The seed point (capital_start) shares day 1's own calendar date (both
    # "start the day" and "end the day" labels collapse to the same date --
    # _equity_curve_with_seed() has no day BEFORE the first trading day to
    # put the seed on). If day 1 is profitable, the seed is a peak
    # immediately superseded by day 1's close on the SAME date, producing a
    # zero-duration, zero-depth phantom "episode" on every such run --
    # filtered out here, not a real drawdown by any definition.
    df = df[df["total_days"] > 0].reset_index(drop=True)
    if df.empty:
        return pd.DataFrame(columns=cols)
    return df.sort_values("total_days", ascending=False).head(top_n).reset_index(drop=True)


def compute_intraday_metrics(result: dict) -> dict:
    """Everything above, bundled from a single run_backtest() result dict
    -- the one call the Dashboard (or any script) needs."""
    trades, daily = result["trades"], result["daily"]
    capital_start = result["capital_start"]
    pos = positions_from_trades(trades)
    monthly = monthly_table(daily, capital_start)
    return {
        "positions": pos,
        "trade_level": trade_level_metrics(pos),
        "r_buckets": r_distribution_buckets(pos),
        "monthly": monthly,
        "monthly_metrics": monthly_metrics(monthly),
        "monthly_buckets": monthly_buckets(monthly),
        "risk_adjusted": risk_adjusted_metrics(monthly, daily, capital_start,
                                               result["start_date"], result["end_date"]),
        "drawdown_episodes": drawdown_episodes(daily, capital_start),
    }
