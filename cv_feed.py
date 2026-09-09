"""
cv_feed.py
-----------------------------------------------------------
Shared alert delivery for the cloud alerters (drop sniper, undercut
monitor). Mirrors DiceScraper\alerts.py on the PC: the caller owns
detection and dedup; this only delivers, and every channel is attempted
independently so one failure cannot silence the others.

    deliver(sb, rows, title, body, email=lambda: cv_email.send(...))

  * feed   — rows go into dice_alerts (event_id "cv:<slug>" so they never
             collide with a Dice id). Receipt flags are written ONLY on the
             rows this call inserted.
  * push   — one Web Push for the whole digest, deep-linked to the first
             row. Zero accepting devices is reported as INCOMPLETE, not as
             success: on 2026-09-08 every cloud feed row ever written (174)
             sat at pushed=false because the runner had no working VAPID
             keys and each alerter logged "skipping push" at INFO.
  * email  — the caller's sender, invoked here so its result lands in the
             same receipt (emailed=true/false on the rows).

Rows left at pushed=false are picked up by the Render app's sweeper
(dicetransfer/cv_push_alert.py), which has verified keys — so a runner
without VAPID secrets still gets its alerts to the phone, just a few
minutes later. Failures never raise; they come back in Delivery.errors and
are logged as ONE warning line, safe for a public log.
-----------------------------------------------------------
"""

import logging
from dataclasses import dataclass, field

import notify_helper

log = logging.getLogger(__name__)


@dataclass
class Delivery:
    feed_ids: list = field(default_factory=list)
    pushed: int = 0
    emailed: bool = False
    errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def deliver(sb, rows: list, title: str, body: str, *, email=None, url: str = None) -> Delivery:
    result = Delivery()
    if not rows:
        return result
    try:
        inserted = sb.table("dice_alerts").insert(rows).execute().data or []
        result.feed_ids = [r["id"] for r in inserted if r.get("id") is not None]
        if not result.feed_ids:
            result.errors.append("feed: no inserted IDs returned")
    except Exception as exc:
        result.errors.append("feed: " + type(exc).__name__)

    target = url or (f"/?tab=alerts&alert={min(result.feed_ids)}"
                     if result.feed_ids else "/?tab=alerts")
    try:
        result.pushed = int(notify_helper.send_push(title, body[:180], target) or 0)
        if not result.pushed:
            result.errors.append("push: no device accepted delivery (VAPID keys unset "
                                 "or no subscription) — Render sweeper will retry")
    except Exception as exc:
        result.errors.append("push: " + type(exc).__name__)

    if email:
        try:
            result.emailed = bool(email())
            if not result.emailed:
                result.errors.append("email: not delivered")
        except Exception as exc:
            result.errors.append("email: " + type(exc).__name__)

    if result.feed_ids:
        try:
            (sb.table("dice_alerts")
             .update({"pushed": result.pushed > 0, "emailed": result.emailed})
             .in_("id", result.feed_ids).execute())
        except Exception as exc:
            result.errors.append("receipt: " + type(exc).__name__)

    if result.errors:
        log.warning("alert delivery incomplete: %s", "; ".join(result.errors))
    else:
        log.info("feed: %d row(s), push to %d device(s), email %s",
                 len(result.feed_ids), result.pushed, "sent" if result.emailed else "off")
    return result
