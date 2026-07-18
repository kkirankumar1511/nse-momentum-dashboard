"""
Central configuration. All secrets come from environment variables / .env file.
NEVER hardcode API keys in this file.

Create a `.env` file next to this one:

    KITE_API_KEY=your_kite_api_key
    KITE_API_SECRET=your_kite_api_secret
    KITE_ACCESS_TOKEN=            # filled daily after login flow
"""

import os
from dotenv import load_dotenv

load_dotenv()

KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")

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

    # Fundamental gate: xbrl_parser's sector-aware value_score/bank_score/
    # nbfc_score/etc total (0-100), computed from primary-source XBRL via
    # fundamentals_agent.fno_value_scan(). 50 is a rough "average-or-better
    # across profitability, health, and growth" bar — tune to taste.
    "min_fundamental_score": 50.0,

    "history_days": 1200,              # calendar days of candles to fetch
}
