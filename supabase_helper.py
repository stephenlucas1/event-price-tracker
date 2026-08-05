"""Minimal Supabase client wrapper. No-op if env vars not set."""

import os
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def client():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception as e:
        log.error("Supabase init failed: %s", e)
        return None


def feed_age_min(sb) -> float | None:
    """Age in minutes of the newest cv_prices row, or None if unknowable."""
    try:
        r = (sb.table("cv_prices").select("updated_at")
             .order("updated_at", desc=True).limit(1).execute())
        rows = r.data or []
        if not rows:
            return None
        from dateutil import parser as dp
        newest = dp.parse(rows[0]["updated_at"])
        return (datetime.now(timezone.utc) - newest).total_seconds() / 60
    except Exception as e:
        log.warning("could not read feed age: %s", e)
        return None


def feed_is_stale(sb, default_max_min: float = 180.0) -> tuple[bool, str]:
    """Should a price ALERT be suppressed because the prices are old?

    Why this exists: on 2026-08-04 CrowdVolt turned on a bot gate, the scan
    stopped writing, and the Hot Board + drop sniper kept emailing every day
    off frozen prices — presenting 30-hour-old lows as current. Alerts that
    look healthy over a dead feed are worse than no alerts, because they get
    acted on. Age is checked at SEND time, by the sender.

    Fails OPEN (a DB hiccup must not silence a real alert); set
    CV_ALERT_MAX_AGE_MIN=0 to disable the guard entirely.
    """
    try:
        max_min = float(os.environ.get("CV_ALERT_MAX_AGE_MIN") or default_max_min)
    except ValueError:
        max_min = default_max_min
    if max_min <= 0:
        return False, "staleness guard disabled"
    age = feed_age_min(sb)
    if age is None:
        return False, "feed age unknown — sending anyway"
    if age > max_min:
        return True, (f"cv_prices is {age:.0f}m old (> {max_min:.0f}m) — the scan "
                      "is not writing; suppressing rather than alerting on frozen prices")
    return False, f"feed is {age:.0f}m old"
