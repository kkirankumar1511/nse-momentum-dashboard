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

import json
import os

import requests
from dotenv import load_dotenv
from pywebpush import WebPushException, webpush

load_dotenv()

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "")

# Browser push (see static/sw.js, push_server.py). VAPID_PRIVATE_KEY is the
# raw url-safe-base64 32-byte EC private key pywebpush accepts directly (no
# PEM file needed); VAPID_PUBLIC_KEY is the matching uncompressed-point
# public key the browser's PushManager.subscribe() needs -- safe to expose
# client-side, that's the point of it being the "public" half. Generate a
# pair with:
#   python -c "from py_vapid import Vapid02; import base64; v=Vapid02(); \
#     v.generate_keys(); pn=v.private_key.private_numbers(); \
#     pub=v.public_key.public_numbers(); \
#     print('VAPID_PRIVATE_KEY=' + base64.urlsafe_b64encode(pn.private_value.to_bytes(32,'big')).rstrip(b'=').decode()); \
#     print('VAPID_PUBLIC_KEY=' + base64.urlsafe_b64encode(b'\x04'+pub.x.to_bytes(32,'big')+pub.y.to_bytes(32,'big')).rstrip(b'=').decode())"
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS_EMAIL = os.getenv("VAPID_CLAIMS_EMAIL", "")

# push_server.py's own port -- dashboard.py's JS derives the full URL from
# this plus the page's own hostname (window.location.hostname), so no
# separate PUSH_SERVER_URL env var is needed.
PUSH_SERVER_PORT = os.getenv("PUSH_SERVER_PORT", "8503")


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


def send_webpush_all(subscriptions: list[dict], title: str, message: str,
                     url: str | None = None) -> list[str]:
    """Sends to every subscription in the pywebpush subscription_info shape
    (see state_db.get_push_subscriptions()). Returns the endpoints found
    dead (410 Gone / 404 -- the device uninstalled the app or revoked
    notifications outside this app) so the caller can delete them from
    state_db; a subscription failing for any OTHER reason (network blip,
    the push service being briefly down) is left alone so a transient
    error doesn't silently unsubscribe a still-valid device.

    No-op (returns []) if VAPID keys aren't configured -- same
    "notifications are optional, never a hard dependency" contract as
    send_push()."""
    if not VAPID_PRIVATE_KEY or not subscriptions:
        return []
    payload = json.dumps({"title": title, "message": message, "url": url or ""})
    dead = []
    for sub in subscriptions:
        try:
            webpush(
                subscription_info=sub, data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{VAPID_CLAIMS_EMAIL or 'admin@localhost'}"})
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                dead.append(sub["endpoint"])
            else:
                print(f"[notify] webpush failed ({status}): {e}")
        except Exception as e:
            print(f"[notify] webpush failed: {e}")
    return dead
