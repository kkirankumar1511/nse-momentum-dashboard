"""
Push notifications via ntfy.sh (https://ntfy.sh) -- no signup, just a
private topic name (subscribe to it in the free ntfy app to receive
pushes on your phone). Used to alert when today's Kite session has
expired and needs a fresh login (see check_kite_token.py, and
state_db.job_run()'s TokenException handling) -- that's the one thing
this app can never automate away, since Zerodha's OAuth login + 2FA
needs an actual human.

Deliberately reads NTFY_TOPIC/DASHBOARD_URL straight from the
environment (its own load_dotenv(), not `import config`) rather than
depending on config.py, which itself imports state_db -- state_db.py
imports this module for the job_run() safety net below, so depending on
config here would create an import cycle (state_db -> notify -> config
-> state_db).

Optional: if NTFY_TOPIC isn't set, every call here is a silent no-op --
notifications are a nice-to-have, never a hard dependency for anything
that calls send_push().
"""

from __future__ import annotations

import os

import requests
from dotenv import load_dotenv

load_dotenv()

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "")


def send_push(title: str, message: str, url: str | None = None,
             priority: str = "default", tags: list[str] | None = None) -> bool:
    """POSTs to ntfy.sh/<topic>. Returns True on success, False on any
    failure (including "not configured") -- callers should never let a
    notification failure break the actual job it's reporting on."""
    if not NTFY_TOPIC:
        return False
    headers = {"Title": title, "Priority": priority}
    if url:
        headers["Click"] = url
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        r = requests.post(f"https://ntfy.sh/{NTFY_TOPIC}",
                          data=message.encode("utf-8"), headers=headers,
                          timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[notify] push failed: {e}")
        return False
