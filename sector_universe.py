"""
Sector relative-strength: identifies which NSE market sectors are currently
outperforming, using REAL historical sector-index price data (not just
today's NSE heatmap snapshot), for use as an optional scoring input
alongside each stock's own momentum.

Why this is point-in-time safe: NSE's heatmap-index/heatmap-symbols APIs
only return today's snapshot, but most of NSE's sectoral indices are
themselves tradeable Kite instruments (segment == "INDICES") with full
historical daily candles -- exactly like the NIFTY 50 benchmark already
used everywhere else in this app. Sector strength is computed with the
same indicators.relative_strength() formula already used for every stock's
rs_3m/rs_6m vs NIFTY, just applied to sector indices instead.

Important: NSE's sectoral indices are NOT a clean partition -- a symbol
can legitimately belong to multiple overlapping baskets (broad umbrella
indices, cap-segment cuts, and strict sub-sectors all coexist; e.g. SBIN
sits in NIFTY BANK, NIFTY FIN SERVICE, NIFTY FINSRV25 50, and NIFTY PSU
BANK simultaneously). stock_sector_rs() resolves this by taking the MAX
relative strength across every basket a stock belongs to, rather than
picking one arbitrarily.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse

import pandas as pd

import indicators
import kite_client
import nse_api

CACHE_PATH = os.path.join("cache", "sector_membership.json")
CACHE_MAX_AGE_DAYS = 7
MANUAL_MAP_PATH = "sector_map_manual.json"  # checked into git, not cache/ --
    # curated reference data, not regenerable from a live API the way
    # cache/ contents are.

HEATMAP_SYMBOLS_URL = "https://www.nseindia.com/api/heatmap-symbols"

# The 21 usable names from NSE's live "Sectoral Indices" heatmap category,
# verified live to have real Kite INDICES-segment instruments (2026-07-19).
# NIFTY CEMENT / NIFTY REITS REALTY excluded -- confirmed no Kite instrument
# exists for either, so no historical candles are obtainable for them.
API_SECTOR_NAMES = [
    "NIFTY AUTO", "NIFTY BANK", "NIFTY FIN SERVICE", "NIFTY FINSRV25 50",
    "NIFTY FMCG", "NIFTY IT", "NIFTY MEDIA", "NIFTY METAL", "NIFTY PHARMA",
    "NIFTY PSU BANK", "NIFTY REALTY", "NIFTY PVT BANK", "NIFTY HEALTHCARE",
    "NIFTY CONSR DURBL", "NIFTY OIL AND GAS", "NIFTY MIDSML HLTH",
    "NIFTY CHEMICALS", "NIFTY500 HEALTH", "NIFTY FINSEREXBNK",
    "NIFTY MS FIN SERV", "NIFTY MS IT TELCM",
]


def _load_manual_map() -> dict[str, str]:
    if os.path.exists(MANUAL_MAP_PATH):
        with open(MANUAL_MAP_PATH) as f:
            return json.load(f)
    return {}


def sector_names() -> list[str]:
    """Union of the API category and whatever the manual file references --
    updating the manual JSON later (as new listings get classified)
    automatically pulls in Kite candles for any new sector name it
    references, with no code change needed here."""
    manual = _load_manual_map()
    return sorted(set(API_SECTOR_NAMES) | set(manual.values()))


def _fetch_api_membership(verbose: bool) -> dict[str, list[str]]:
    """Raises if every sector request fails (mirrors fno_universe.py's
    fetch_fno_symbols_live -- total failure is the caller's problem to
    fall back on, a handful of individual sector failures is not."""
    s = nse_api.session()
    membership: dict[str, list[str]] = {}
    ok_sectors = 0
    for sec in API_SECTOR_NAMES:
        url = f"{HEATMAP_SYMBOLS_URL}?type=Sectoral%20Indices&indices={urllib.parse.quote(sec)}"
        r = s.get(url, timeout=15)
        if r.status_code != 200:
            if verbose:
                print(f"[sector_universe] {sec}: heatmap-symbols failed "
                     f"({r.status_code})")
            continue
        ok_sectors += 1
        for row in r.json():
            sym = row.get("symbol")
            if sym:
                membership.setdefault(sym, []).append(sec)
        time.sleep(0.3)
    if ok_sectors == 0:
        raise RuntimeError("all sector heatmap-symbols requests failed")
    return membership


def get_sector_membership(force_refresh: bool = False,
                          verbose: bool = True) -> dict[str, list[str]]:
    """symbol -> list of sector index names it belongs to. NOT a single
    sector -- see module docstring on why this can't be a clean partition.

    Two sources, merged: NSE's live "Sectoral Indices" heatmap category
    (cached CACHE_MAX_AGE_DAYS, ~148 F&O symbols, some in 2+ sectors), plus
    sector_map_manual.json (~50 more symbols the API's category doesn't
    classify -- newer/thematic listings covering PSU, defence, energy,
    infra, etc). The manual file only ever ADDS symbols the API left
    uncovered, never overrides an API-derived entry.
    """
    age_days = ((time.time() - os.path.getmtime(CACHE_PATH)) / 86400
               if os.path.exists(CACHE_PATH) else 1e9)
    if not force_refresh and age_days < CACHE_MAX_AGE_DAYS:
        with open(CACHE_PATH) as f:
            api_membership = json.load(f)
    else:
        try:
            api_membership = _fetch_api_membership(verbose)
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            with open(CACHE_PATH, "w") as f:
                json.dump(api_membership, f, indent=1)
        except Exception as e:
            if verbose:
                print(f"[sector_universe] live fetch failed ({e})")
            if os.path.exists(CACHE_PATH):
                with open(CACHE_PATH) as f:
                    api_membership = json.load(f)
                if verbose:
                    print(f"[sector_universe] using stale cache "
                         f"({age_days:.0f}d old)")
            else:
                api_membership = {}

    manual = _load_manual_map()
    membership = {sym: list(secs) for sym, secs in api_membership.items()}
    manual_added = 0
    for sym, sec in manual.items():
        if sym not in membership:
            membership[sym] = [sec]
            manual_added += 1

    if verbose:
        overlap = sum(1 for secs in membership.values() if len(secs) > 1)
        print(f"[sector_universe] membership: {len(api_membership)} via API "
             f"({overlap} in 2+ sectors), {manual_added} via manual "
             f"supplement, {len(membership)} total")

    return membership


def _naive(frame: pd.DataFrame) -> pd.DataFrame:
    """Kite's timestamps are tz-aware (IST); backtest.py's stock candles and
    benchmark are normalized to tz-naive right after load (see
    load_candles_cached), so comparing/slicing against them raises
    TypeError unless sector candles get the same treatment here."""
    if not frame.empty and frame.index.tz is not None:
        frame = frame.copy()
        frame.index = frame.index.tz_localize(None)
    return frame


def fetch_sector_index_candles(days: int = 1200) -> dict[str, pd.DataFrame]:
    """Real historical daily candles for every sector index (each one's own
    Kite INDICES-segment instrument, same as the NIFTY 50 benchmark) -- this
    is what makes point-in-time sector strength possible, not just today's
    NSE heatmap snapshot."""
    out = {}
    for name in sector_names():
        try:
            out[name] = _naive(kite_client.fetch_index_candles(name, days))
        except Exception as e:
            print(f"[sector_universe] {name}: candle fetch failed: {e}")
            out[name] = pd.DataFrame()
        time.sleep(0.35)
    return out


def sector_rs_asof(sector_candles: dict[str, pd.DataFrame], bench: pd.DataFrame,
                   date, lookback_days: int) -> pd.Series:
    """Point-in-time relative strength (vs NIFTY 50) for every sector index,
    as of `date` -- the exact indicators.relative_strength() formula already
    used for every stock's rs_3m/rs_6m, just applied to sector indices.
    Sectors with insufficient history as of this date are dropped, not
    defaulted -- callers (stock_sector_rs) treat a missing sector as
    unknown, not zero.

    Normalizes tz-awareness internally (both `bench` and each sector frame)
    rather than trusting every caller to have done it -- some callers go
    through backtest.load_candles_cached (already tz-naive), others call
    kite_client.benchmark_candles directly (still tz-aware); a single
    caller forgetting that distinction shouldn't crash this function.
    """
    date = pd.Timestamp(date).tz_localize(None) if pd.Timestamp(date).tz else pd.Timestamp(date)
    bench = _naive(bench)
    bench_close = bench.loc[:date, "close"] if not bench.empty else bench
    scores = {}
    for name, df in sector_candles.items():
        if df.empty:
            continue
        sliced = _naive(df).loc[:date]
        rs = indicators.relative_strength(sliced["close"], bench_close, lookback_days)
        if pd.notna(rs):
            scores[name] = rs
    return pd.Series(scores, dtype=float).sort_values(ascending=False)


def stock_sector_rs(symbol: str, membership: dict[str, list[str]],
                    sector_rank: pd.Series) -> float | None:
    """A stock's sector-strength signal: the MAX relative strength across
    every sector basket it belongs to -- not a single arbitrarily-assigned
    sector (see module docstring). None if the stock has no membership, or
    none of its sectors have a computed rank yet (e.g. too early in a
    backtest for that sector's own lookback window)."""
    secs = membership.get(symbol, [])
    vals = [sector_rank[s] for s in secs if s in sector_rank.index]
    return max(vals) if vals else None


if __name__ == "__main__":
    m = get_sector_membership(force_refresh=True)
    print(f"\n{len(sector_names())} sector indices tracked: {sector_names()}")
