"""
Central configuration. All secrets come from environment variables / .env file.
NEVER hardcode API keys in this file.

Create a `.env` file next to this one:

    KITE_API_KEY=your_kite_api_key
    KITE_API_SECRET=your_kite_api_secret
    KITE_ACCESS_TOKEN=            # filled daily after login flow
    LLM_PROVIDER=ollama           # ollama | groq | openrouter | together | anthropic
    LLM_MODEL=                    # blank = sensible default per provider
    LLM_API_KEY=                  # only for hosted providers
    ANTHROPIC_API_KEY=            # optional paid fallback
"""

import os
from dotenv import load_dotenv

load_dotenv()

KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")
KITE_ACCESS_TOKEN = os.getenv("KITE_ACCESS_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")   # optional, paid

# ---------------------------------------------------------------------------
# LLM provider — open-source / local by default. See llm.py for the full list.
#   LLM_PROVIDER=ollama      local, free, private (recommended default)
#   LLM_PROVIDER=groq        hosted open weights, free tier, fast
#   LLM_PROVIDER=openrouter  free ":free" open models
#   LLM_PROVIDER=anthropic   paid fallback
# ---------------------------------------------------------------------------
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama")
LLM_MODEL = os.getenv("LLM_MODEL", "")          # blank = provider default
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")

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

    # Long-year breakout priority: stocks breaking above a multi-year high
    # after a long base. George & Hwang's 52w-high effect strengthens at
    # longer highs, and a 1-3 year base means no overhead supply of trapped
    # sellers above the breakout level.
    "breakout_lookback_days": 750,     # ~3 trading years scanned for the prior high
    "breakout_confirm_window": 20,     # breakout must be recent (last ~1 month)
    "breakout_min_base_days": 126,     # prior high must be >= ~6 months old
    "breakout_bonus": 0.0,             # disabled: backtest showed no edge over
                                        # non-breakout trades (n=4 sample though —
                                        # revisit if the sample grows). 0 = no
                                        # bonus/priority tier, see screener.score().
    "breakout_bonus_cap_years": 2.0,   # bonus caps at 2 years of base (unused while 0)

    # Pre-breakout watchlist: stocks still UNDER a multi-year high (overhead
    # supply not yet cleared) but within this fraction of it, with a base
    # already >= breakout_min_base_days old. Flagged `near_breakout` and
    # HARD-EXCLUDED from buy candidates in screener.apply_gates -- the whole
    # point of waiting is that a long base can fail right at the ceiling, so
    # entry must happen only after a confirmed close above the prior high.
    "near_breakout_pct": 0.90,

    # Entry timing: "calendar" buys immediately at the monthly rebalance
    # close (an arbitrary day relative to the stock's own short-term swing).
    # "ema_pullback" instead waits for gate-passers to dip to/through the
    # pullback EMA and close back up before buying -- a controlled entry on
    # strength returning instead of chasing whatever the calendar date is.
    # Trade-off: misses stocks that run straight up without ever pulling back.
    "entry_mode": "calendar",          # "calendar" | "ema_pullback"
    "pullback_ema_period": 20,
    "pullback_tolerance_pct": 3.0,     # bar's low within this % above the EMA counts as "touched"
    "history_days": 1200,              # calendar days of candles to fetch
}
