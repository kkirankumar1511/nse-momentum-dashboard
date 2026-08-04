"""
Standalone daily check: is today's Kite access token still valid? Run
once each morning (nse-token-check.timer, ~07:00 IST -- well after
Kite's ~6 AM daily token reset and well before the 09:16 gap-check needs
a working session) and pushes a notification with a direct link to the
dashboard if it's expired, so there's time to log in before market open
instead of only discovering it when a scheduled job fails.

Doesn't touch state_db.job_runs -- this isn't a rebalance/gap-check/
fundamentals job, just a proactive heads-up. See state_db.job_run()'s
own TokenException handling for the safety-net case where a scheduled
job discovers an expired token mid-run instead (covers this check not
having run, or the token expiring/getting revoked later in the day).
"""

from __future__ import annotations

import datetime as dt

from kiteconnect.exceptions import TokenException

import config
import kite_client
import notify


def _alert():
    print(f"{dt.datetime.now():%d %b %Y %H:%M:%S} Kite token EXPIRED -- "
         f"sending push notification.")
    notify.send_push(
        title="KK Trading -- Kite login needed",
        message="Today's Kite session has expired. Log in before the "
                "09:16 gap-check / market open.",
        url=notify.DASHBOARD_URL or None,
        priority="urgent", tags=["warning", "key"])


def main():
    if not config.KITE_ACCESS_TOKEN:
        _alert()
        return
    try:
        kite_client.get_kite().profile()
        print(f"{dt.datetime.now():%d %b %Y %H:%M:%S} Kite token OK.")
    except TokenException:
        _alert()
    except Exception as e:
        print(f"{dt.datetime.now():%d %b %Y %H:%M:%S} Token check itself "
             f"failed (not necessarily an expired token): {e}")


if __name__ == "__main__":
    main()
