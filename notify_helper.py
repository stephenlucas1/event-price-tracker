"""Web Push notification sender. No-op if VAPID keys not set."""

import os
import json
import logging

log = logging.getLogger(__name__)


def _claim() -> dict:
    email = os.environ.get("VAPID_CLAIM_EMAIL", "mailto:admin@example.com")
    if not email.startswith("mailto:"):
        email = "mailto:" + email
    return {"sub": email}


def _load_subscriptions() -> list:
    import supabase_helper as db
    sb = db.client()
    if not sb:
        return []
    try:
        r = sb.table("push_subscriptions").select("endpoint,subscription").execute()
        return r.data or []
    except Exception as e:
        log.error("load_subscriptions failed: %s", e)
        return []


def _delete_subscription(endpoint: str):
    import supabase_helper as db
    sb = db.client()
    if not sb:
        return
    try:
        sb.table("push_subscriptions").delete().eq("endpoint", endpoint).execute()
    except Exception as e:
        log.error("delete_subscription failed: %s", e)


def send_push(title: str, body: str, url: str = "/") -> int:
    priv = os.environ.get("VAPID_PRIVATE_KEY", "")
    pub  = os.environ.get("VAPID_PUBLIC_KEY", "")
    if not priv or not pub:
        log.info("VAPID keys not set — skipping push")
        return 0

    try:
        from pywebpush import webpush, WebPushException
    except Exception as e:
        log.error("pywebpush not installed: %s", e)
        return 0

    payload = json.dumps({"title": title, "body": body, "url": url})
    sent = 0
    for row in _load_subscriptions():
        try:
            sub = json.loads(row["subscription"])
        except Exception:
            continue
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=priv,
                vapid_claims=dict(_claim()),
            )
            sent += 1
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                _delete_subscription(row.get("endpoint", ""))
            else:
                log.error("Push failed: %s", e)
        except Exception as e:
            log.error("Push error: %s", e)

    log.info("Push sent to %d device(s)", sent)
    return sent
