"""Sandbox entry point for QA testing: runs the REAL dashboard.py/
live_rebalance.py/screener.py/backtest.py business logic against a fully
mocked kite_client (scripts/sandbox_mock_kite.py) and a throwaway state.db
(cache/state_sandbox.db) -- zero real Kite API calls, zero real orders,
zero risk to the real cache/state.db.

Run with:  streamlit run scripts/run_sandbox.py --server.port 8502
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SANDBOX_DB = os.path.join("cache", "state_sandbox.db")

# Fake but truthy Kite creds so config.py's bootstrap seeds them into the
# SANDBOX db (only used the first time the sandbox db is created) instead
# of reading/needing any real credentials.
os.environ.setdefault("KITE_API_KEY", "sandbox_api_key")
os.environ.setdefault("KITE_API_SECRET", "sandbox_api_secret")
os.environ.setdefault("KITE_ACCESS_TOKEN", "sandbox_access_token")

import state_db
state_db.DB_PATH = SANDBOX_DB

import config  # noqa: E402  (seeds sandbox db with the fake creds above)

from scripts import sandbox_mock_kite
sandbox_mock_kite.patch_kite_client()

config.refresh_universe()

# backtest.py's own raw candle cache (cache/{symbol}.csv, cache/_NIFTY.csv --
# written by load_candles_cached()) is a SEPARATE caching layer from the 4
# pickles redirected below and from state_db.DB_PATH -- nothing routes it
# through either of those, so a sandboxed "Run backtest" click (kite_client
# mocked) would otherwise write fake synthetic candles straight into the
# real shared cache/ directory, silently corrupting every other real
# backtest run's data until the cache "expires" or is manually refetched.
# Confirmed to actually happen: an earlier sandbox QA pass's Backtest-page
# test overwrote cache/*.csv (NIFTY included) with sandbox_mock_kite's
# synthetic series, undetected until a much later real backtest produced
# an impossible -24% to -39% NIFTY CAGR. Redirecting CACHE_DIR here closes
# that gap the same way the pickle substitution below closes its own.
import backtest as bt
bt.CACHE_DIR = os.path.join("cache", "sandbox_candles")
os.makedirs(bt.CACHE_DIR, exist_ok=True)

import scripts.seed_sandbox_data as seed_sandbox_data  # noqa: E402
seed_sandbox_data.seed_if_empty()

with open("dashboard.py", encoding="utf-8") as f:
    _dashboard_src = f.read()

# Redirect dashboard.py's own hardcoded cache/*.pkl paths to sandbox-only
# filenames -- these aren't behind state_db.DB_PATH (they're separate
# pickle files dashboard.py writes to directly on "Run screen"/"Run
# backtest"/etc.), so without this substitution a real click of those
# buttons in the sandbox would overwrite the REAL cached screener/
# backtest/fundamentals results with fake data. Substituting the source
# text before exec (rather than chdir-ing elsewhere) keeps assets/ and
# .streamlit/ resolving normally, since those DO need the real repo root.
for _real_name, _sandbox_name in [
    ("screen.pkl", "sandbox_screen.pkl"),
    ("fno_value_scores.pkl", "sandbox_fno_value_scores.pkl"),
    ("backtest_result.pkl", "sandbox_backtest_result.pkl"),
    ("fundamentals_history.pkl", "sandbox_fundamentals_history.pkl"),
]:
    _dashboard_src = _dashboard_src.replace(
        f'os.path.join("cache", "{_real_name}")',
        f'os.path.join("cache", "{_sandbox_name}")')

exec(compile(_dashboard_src, "dashboard.py", "exec"), {"__name__": "__main__"})
