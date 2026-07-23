"""
Central configuration.

Kite Connect credentials (api_key/api_secret/access_token) and the
dashboard login live in cache/state.db (see state_db.py) -- .env is only
used to BOOTSTRAP them once, the first time this app runs with an empty
database. After that first run, updating .env does nothing for these
values; use the dashboard's own settings forms (Cockpit's "Change
dashboard password" / "Kite API settings") instead, since the DB is the
live source of truth from then on.

Create a `.env` file next to this one (only needed for that first run):

    KITE_API_KEY=your_kite_api_key
    KITE_API_SECRET=your_kite_api_secret
    KITE_ACCESS_TOKEN=            # filled daily after login flow
    DASHBOARD_USERNAME=your_username     # optional -- defaults to "Admin"
    DASHBOARD_PASSWORD=your_password     # optional -- defaults to "Admin"
"""

import os
from dotenv import load_dotenv

import state_db

load_dotenv()

state_db.ensure_kite_credentials_seeded(
    os.getenv("KITE_API_KEY", ""), os.getenv("KITE_API_SECRET", ""),
    os.getenv("KITE_ACCESS_TOKEN", ""))
_kite_creds = state_db.get_kite_credentials()

KITE_API_KEY = _kite_creds["api_key"]
KITE_API_SECRET = _kite_creds["api_secret"]
KITE_ACCESS_TOKEN = _kite_creds["access_token"] or ""

# Dashboard login gate (dashboard.py) -- placeholders on purpose, not a real
# credential store. This app places real orders and shows real fund
# balances, so change these via the Cockpit's "Change dashboard password"
# section before exposing it beyond your own machine -- "Admin"/"Admin" is
# one of the most commonly attacked default credential pairs that exists.
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "Admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "Admin")

# ---------------------------------------------------------------------------
# Universe: NSE F&O-eligible stocks only (~210 names), fetched live from
# https://www.nseindia.com/api/underlying-information and cached weekly.
#
# Why F&O-only: these are NSE's most liquid stocks (the exchange's own
# eligibility rules screen for market cap and quarter-sigma order size), so
# slippage stays low — and slippage is precisely what erodes momentum edges
# in live trading. They can also be hedged with options if a position sours.
#
# Override with a manual list here if you ever want to test a custom set.
# ---------------------------------------------------------------------------
import fno_universe as _fno

UNIVERSE_OVERRIDE: list[str] = []          # non-empty = use this instead


def get_universe(refresh: bool = False) -> list[str]:
    if UNIVERSE_OVERRIDE:
        return UNIVERSE_OVERRIDE
    return _fno.tradable_on_kite(_fno.get_fno_universe(force_refresh=refresh))


# Lazily-resolved default universe (safe at import time — falls back to the
# bundled snapshot if NSE/Kite are unreachable).
UNIVERSE = _fno.get_fno_universe(verbose=False)

BENCHMARK = "NSE:NIFTY 50"   # for relative strength

# ---------------------------------------------------------------------------
# Strategy parameters (see README for the research behind each)
# ---------------------------------------------------------------------------
STRATEGY = {
    # Momentum lookbacks (Jegadeesh & Titman 1993: 3-12m formation works for
    # 3-6m holding; we skip the most recent week to dodge short-term reversal)
    "mom_lookback_days_short": 63,     # ~3 months
    "mom_lookback_days_long": 126,     # ~6 months
    "skip_recent_days": 5,

    # 52-week high proximity (George & Hwang 2004)
    "near_high_threshold": 0.85,       # price >= 85% of 52w high to qualify

    # Trend structure
    "ema_fast": 50,
    "ema_slow": 200,

    # RSI regime: momentum names trade 40-80; avoid parabolic >80 entries
    "rsi_period": 14,
    "rsi_min": 45,
    "rsi_max": 78,

    # Volume confirmation: 20d avg volume vs 60d avg volume
    "volume_expansion_min": 1.0,

    # Risk management
    "atr_period": 14,
    "atr_stop_multiple": 2.5,          # initial stop = entry - 2.5*ATR
    "risk_per_trade_pct": 0.5,         # % of capital risked per position
    "max_positions": 10,

    # Trailing stop (chandelier-style): ratchets the stop up to
    # highest_close_since_entry - trailing_atr_multiple*ATR as a position
    # gains, never back down. A real 5-year sweep found this forms an
    # inverted-U across multiples (too narrow whipsaws, too wide barely
    # trails at all), peaking at 4.0 (CAGR 24.30% vs baseline 22.51%,
    # Sharpe 1.73 vs 1.50, max DD -14.37% vs -18.06%) -- see the README's
    # "Trailing stop" section for the full sweep, including that adding
    # either the fundamental gate or the sector bonus on top of it gave
    # back the improvement rather than adding to it. This is the one
    # experimental feature from that round that earned a spot in the
    # default strategy -- sector_bonus_weight and the (also-tried,
    # removed) regime filter did not.
    "trailing_stop_enabled": True,
    "trailing_atr_multiple": 4.0,

    # Fundamental gate: xbrl_parser's sector-aware value_score/bank_score/
    # nbfc_score/etc total (0-100), computed from primary-source XBRL via
    # fundamentals_agent.fno_value_scan(). 50 is a rough "average-or-better
    # across profitability, health, and growth" bar — tune to taste.
    "min_fundamental_score": 50.0,

    # Sector relative-strength bonus (see sector_universe.py): tilts ranking
    # toward stocks in currently-outperforming sectors. 0 = off, matching
    # this codebase's pattern of shipping a new scoring dimension disabled
    # until an A/B backtest earns it (see the removed breakout-bonus tier).
    "sector_bonus_weight": 0.0,
    "sector_rs_lookback_days": 126,    # matches mom_lookback_days_long

    "history_days": 1200,              # calendar days of candles to fetch
}
