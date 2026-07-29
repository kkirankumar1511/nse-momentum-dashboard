"""
Background jobs for dashboard.py's long-running scans (Screener's "Run
screen", Live Rebalance's "Run today's scan") that should keep running even
if you switch to a different page, and survive across page reruns.

This MUST be its own imported module, not code living in dashboard.py
itself. Streamlit re-executes dashboard.py (the entrypoint script) top to
bottom on every single rerun -- only imported modules are cached in
sys.modules after their first import. A module-level dict declared inside
dashboard.py itself gets reinitialized to empty on every rerun, including
the very next rerun after a job was just started -- which silently
undermined the whole point of this (a real bug found 2026-07-29: jobs never
appeared "running" one rerun later, so the disable-while-running button and
the "already in progress" message never fired). Living in a properly
imported module instead means Python's normal import caching keeps _JOBS
alive across every one of dashboard.py's repeated top-to-bottom re-runs,
the same mechanism that already keeps state_db/kite_client's own
module-level state persistent.

_JOBS is process-global (not st.session_state, which isn't safe to write
from a non-main thread, and not per-session) -- matches this app's
single-user assumption elsewhere.
"""

from __future__ import annotations

import datetime as dt
import threading

_JOBS: dict[str, dict] = {}


def start_background_job(key: str, fn, *args, **kwargs) -> bool:
    """Runs fn(*args, **kwargs, progress_cb=...) in a background thread.
    No-ops (returns False) if a job with this key is already running --
    otherwise returns True. `progress_cb(stage, frac)` and the eventual
    result/error are stashed on the job dict for get_background_job() to
    read on a later poll."""
    existing = _JOBS.get(key)
    if existing is not None and existing["thread"].is_alive():
        return False

    job = {"thread": None, "done": False, "result": None, "error": None,
          "progress": (0.0, "Starting..."), "started_at": dt.datetime.now()}

    def _progress_cb(stage, frac):
        job["progress"] = (frac, stage)

    def _runner():
        try:
            job["result"] = fn(*args, progress_cb=_progress_cb, **kwargs)
        except Exception as e:
            job["error"] = e
        finally:
            job["done"] = True

    job["thread"] = threading.Thread(target=_runner, daemon=True)
    _JOBS[key] = job
    job["thread"].start()
    return True


def get_background_job(key: str) -> dict | None:
    return _JOBS.get(key)


def clear_background_job(key: str) -> None:
    _JOBS.pop(key, None)
