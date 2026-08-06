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

    # Optional push notifications (see notify.py) -- read directly by
    # notify.py, not by this file. Leave VAPID_PRIVATE_KEY blank to disable;
    # nothing else depends on it being set.
    DASHBOARD_URL=               # e.g. https://your-host.ts.net:8501/ --
                                  # included as a clickable link in alerts

    # Browser push (see notify.py's docstring for the keygen one-liner)
    VAPID_PRIVATE_KEY=
    VAPID_PUBLIC_KEY=
    VAPID_CLAIMS_EMAIL=          # any contactable email -- not verified,
                                  # just required by the Web Push spec
    PUSH_SERVER_PORT=8503        # push_server.py's own port
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
    base = (UNIVERSE_OVERRIDE if UNIVERSE_OVERRIDE else
           _fno.tradable_on_kite(_fno.get_fno_universe(force_refresh=refresh)))
    skipped = set(state_db.get_skipped_symbols())
    return [s for s in base if s not in skipped]


def refresh_universe() -> None:
    """Recomputes UNIVERSE and UNIVERSE_RAW in place, applying the current
    skip list (state_db.skipped_symbols, editable from Admin -- "Skip
    stocks from scanner"). Called once below at import time, and again
    after every Admin-page skip/un-skip so the change takes effect
    immediately for every module that reads config.UNIVERSE -- screener.py,
    backtest.py, fundamentals_agent.py, dashboard.py all do `import config`
    and then read `config.UNIVERSE` fresh at call time (never `from config
    import UNIVERSE`), so reassigning the name here is all that's needed;
    same live-update pattern as config.STRATEGY.update() elsewhere in this
    app -- no dashboard/service restart required.

    UNIVERSE_RAW is the full F&O-eligible list, NEVER skip-filtered --
    skipping a stock is a "don't scan/trade this" preference, not a claim
    that NSE's actual F&O list is smaller, so anything just reporting the
    exchange's real count (e.g. the sidebar's "F&O universe: N stocks")
    should read UNIVERSE_RAW, not UNIVERSE."""
    global UNIVERSE, UNIVERSE_RAW
    UNIVERSE_RAW = _fno.get_fno_universe(verbose=False)
    skipped = set(state_db.get_skipped_symbols())
    UNIVERSE = [s for s in UNIVERSE_RAW if s not in skipped]


# Lazily-resolved default universe (safe at import time — falls back to the
# bundled snapshot if NSE/Kite are unreachable). UNIVERSE excludes anything
# in the manual skip list; UNIVERSE_RAW never does -- see refresh_universe().
UNIVERSE: list[str] = []
UNIVERSE_RAW: list[str] = []
refresh_universe()

BENCHMARK = "NSE:NIFTY 50"   # for relative strength

# ---------------------------------------------------------------------------
# Strategy parameters (see README for the research behind each).
#
# _STRATEGY_DEFAULTS below is only the SEED/fallback -- state_db.strategy_config
# is the live source of truth from first run onward (same pattern as Kite
# credentials above), editable from the dashboard's Admin page without a code
# change or restart. Change the research defaults themselves here; change your
# own live values from the Admin page.
# ---------------------------------------------------------------------------
_STRATEGY_DEFAULTS = {
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

    # RSI regime: momentum names trade 40-80; avoid parabolic >80 entries.
    # rsi_max raised 78 -> 80 after an A/B backtest (5y, max_positions=4):
    # CAGR 7.36->8.15%, Sharpe 1.14->1.25, max DD -6.62->-6.51%, profit
    # factor 1.9->2.06 -- a clean, consistent improvement (flat-or-better in
    # 5 of 6 calendar years), not one lucky year. A separate idea tested
    # alongside this one -- relaxing rsi_max specifically for ALREADY-HELD
    # positions (so overbought alone wouldn't trigger a sell) -- made every
    # metric worse and was discarded entirely, same as the regime filter and
    # beta scoring before it: this simple threshold-only change is the one
    # that earned a spot.
    "rsi_period": 14,
    "rsi_min": 45,
    "rsi_max": 80,

    # Backtest-only (for now): decouples the RSI ceiling used to decide
    # whether an ALREADY-HELD position should be sold from the entry-band
    # check above. See the rsi_max comment just above -- this exact idea
    # (widen the ceiling for held positions only, so overbought alone
    # wouldn't trigger a sell) was already A/B tested once and made every
    # metric worse, hence off by default. Re-exposed here, wired into the
    # Backtest UI, for re-testing against today's data/universe rather
    # than trusting that one past result forever.
    "rsi_exit_gate_enabled": False,
    "rsi_exit_max": 80,

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

    # Push a ratcheted trailing stop straight to the real broker GTT the
    # moment it's computed (live_rebalance.py's compute_stop_updates),
    # rather than waiting for a manual "Apply stop updates" click --
    # same automation level as the gap-down safety check. Deliberately
    # not something that needed a backtest: this only changes WHO/WHEN
    # pushes an already-decided stop value to the broker, not the stop
    # formula or any trading decision itself, and it only ever tightens
    # risk (ratchets up), never loosens it or places a new order.
    "auto_apply_stop_updates": True,

    # Fundamental gate: xbrl_parser's sector-aware value_score/bank_score/
    # nbfc_score/etc total (0-100), computed from primary-source XBRL via
    # fundamentals_agent.fno_value_scan(). 50 is a rough "average-or-better
    # across profitability, health, and growth" bar — tune to taste.
    "min_fundamental_score": 50.0,
    # Turned on after a 5-year A/B backtest (equal-weight capital sizing --
    # matches live_rebalance.py's actual sizing -- max_positions=10): a real
    # trade-off, not a clean win -- CAGR 44.66%->42.72%, Sharpe 1.62->1.60,
    # profit factor 2.15->2.07, but max drawdown improves -28.65%->-24.47%.
    # Wins 3 of 6 calendar years (2021/2022/2025, the calmer ones), loses in
    # the two blowout years 2023/2024 and 2026 YTD. Kept on for the
    # drawdown reduction -- re-run the A/B yourself before flipping back.
    # The fundamental score is always shown (Screener, Live Rebalance)
    # whenever fundamentals data is available, regardless of this flag --
    # see screener.apply_gates()'s docstring.
    "fundamental_gate_enabled": True,

    # Fundamental-score ranking tilt (screener.score(), on top of the gate
    # above -- this affects WHERE a gate-passing candidate ranks, the gate
    # only ever excludes/includes). A 5-year A/B sweep (equal-weight
    # sizing, max_positions=10) found an inverted-U peaking near 0.5:
    # weight 0->0.5->1.0->2.0 gives CAGR 43.70%->43.03%->38.88%->27.93%,
    # Sharpe 1.62->1.64->1.56->1.26, but max drawdown improves at 0.5
    # specifically (-24.61%->-20.30%) before getting worse again at 1.0
    # (-26.72%). 0.5 is a near-wash on CAGR/win-rate (51.7%->50.6%) for a
    # real ~4pp drawdown improvement -- kept for the same reasoning as the
    # gate itself. Anything above 0.5 is a clear net negative.
    "fundamental_bonus_weight": 0.5,

    # Sector relative-strength bonus (see sector_universe.py): tilts ranking
    # toward stocks in currently-outperforming sectors. 0 = off, matching
    # this codebase's pattern of shipping a new scoring dimension disabled
    # until an A/B backtest earns it (see the removed breakout-bonus tier).
    "sector_bonus_weight": 0.0,
    "sector_rs_lookback_days": 126,    # matches mom_lookback_days_long

    "history_days": 1200,              # calendar days of candles to fetch

    # Equal-weight capital allocation (screener.capital_position_size) vs.
    # the risk-based sizing above (screener.position_size, what backtest.py
    # uses when this is off). False reproduces the original behavior exactly
    # -- opt-in until an A/B backtest earns it a spot as the default, same
    # pattern as sector_bonus_weight/trailing_stop_enabled. live_rebalance.py
    # always uses capital_position_size regardless of this flag (a live/UX
    # decision made independently -- see its own module for why); this flag
    # only controls backtest.py's simulation, for comparing the two.
    "capital_equal_weight_sizing": False,

    # screener.allocate_equal_weight_buys(): a from-scratch replacement for
    # capital_position_size()'s one-symbol-at-a-time sizing, which greedily
    # skipped an unaffordable top-ranked candidate for whatever cheaper,
    # lower-ranked stock it found next (this is what let a rank #9 stock
    # get bought over rank #1-8 ones when cash was thin -- the real
    # incident that prompted this feature). Decides the WHOLE day's/run's
    # allocation in one pass instead: cross-slot borrowing within
    # equal_weight_tolerance_pct, a partial (not skipped) fill when even
    # borrowing can't reach full target, a hard stop (no substitution) only
    # once nothing at all is affordable, and topping up existing
    # under-target holdings with any leftover cash. A 5-year A/B (equal-
    # weight sizing, max_positions=10, on top of the fundamental gate +
    # bonus weight above) found tolerance=0.20 beats the old per-symbol
    # fill on every metric at once: CAGR 43.06%->44.39%, Sharpe 1.64->1.67,
    # max drawdown -20.30%->-19.58%, profit factor 2.10->2.12 -- verified
    # via instrumented run that the top-up path (103/1236 days) and
    # partial-fill path (127/1236 days) both genuinely fire, not just
    # theoretically reachable. live_rebalance.py uses this whenever it's
    # on; False reverts to capital_position_size's original one-at-a-time
    # behavior for rollback.
    "advanced_equal_weight_sizing": True,
    "equal_weight_tolerance_pct": 0.20,

    # Fully automate placing the proposed sells/buys/top-ups as real
    # orders, immediately after the SCHEDULED daily rebalance job computes
    # them (live_rebalance.py's main() only -- deliberately NOT the
    # dashboard's manual "Run today's scan" button, so clicking that to
    # look at today's candidates never itself fires real orders; same
    # split as check_gap_down_stops() being scheduled-only automation).
    # False by default -- unlike auto_apply_stop_updates (which only ever
    # tightens an existing stop, low-risk), this places brand-new orders
    # and exits real positions, so it stays an explicit opt-in only once
    # you're confident in the proposal quality. When on, uses the exact
    # same execute_sells()/execute_buys()/execute_top_ups() functions the
    # dashboard's manual buttons call, so the two paths can't drift apart.
    "auto_execute_trades": False,

    # Rebalance cadence: "daily" re-evaluates the sell/keep-zone rule every
    # time the scheduled scan runs (Mon-Fri); "monthly" only evaluates it
    # on the first trading day of each calendar month (nse_holidays.
    # is_month_start_trading_day), same definition backtest.py's monthly
    # rb_dates uses. New buys/top-ups always fill whatever slots are
    # already open regardless of this setting -- only the SELL decision
    # (rank/200-EMA check) is gated, matching how backtest.py's own daily
    # loop works (watchlist refreshed monthly, but a freed slot gets
    # filled the same day it opens, not held until next rebalance).
    # "daily" is the current default: real 2016-2026 data + a 5-seed
    # synthetic multi-regime test both found it meaningfully reduces max
    # drawdown (roughly -50% -> -35% over 10 years) at a real but smaller
    # cost to CAGR -- a priced trade-off, not a free win, so this is worth
    # re-checking against your own risk tolerance rather than assuming
    # it's forever the right choice.
    "rebalance_cadence": "daily",
}

STRATEGY = state_db.get_strategy_config(_STRATEGY_DEFAULTS)
