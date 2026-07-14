"""
axs_monitor.py — cloud variant (GitHub Actions)
-----------------------------------------------------------
Port of the local DiceScraper axs_monitor. Same alert rules, but stateless
between runs: the Supabase `axs_events` table is BOTH the watch list and the
previous-poll snapshot. The local machine syncs the watch list (which URLs,
labels, explicit thresholds) into that table; this job never adds/removes
watched events, only refreshes their snapshots.

Alert conditions (vs the row's stored previous snapshot):
  * status flips into a buyable state
  * official AXS resale appears on the page
  * meaningful price drop (>= $2 and >= 5%)
  * from-price crosses down through the cheap threshold
  * presale/on-sale datetimes change

Thresholds: row.cheap_threshold if set, else the lowest Lysted list price
for the same "{event_name} {YYYY-MM-DD}" label (undercut detection).

Alerts go out by email (Gmail SMTP). Env:
  SUPABASE_URL, SUPABASE_KEY   required
  GMAIL_USER, GMAIL_APP_PASSWORD  email sender (no email if unset)
  ALERT_TO                     recipient (default: GMAIL_USER)
  AXS_DELAY                    seconds between event fetches (default 4)

NOTE: this runs in a PUBLIC repo — logs are world-readable. Log only row
indexes, HTTP statuses and alert counts; never names, URLs or emails.
"""

import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from curl_cffi import requests as creq

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)
for _n in ("httpx", "httpcore", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)

IMPERSONATE = "safari17_0"   # chrome impersonation gets 403 from axs.com
FETCH_PAUSE_S = float(os.environ.get("AXS_DELAY") or "4")
BUYABLE = {"buy tickets", "on sale", "buy now", "buy"}

# snapshot fields persisted in axs_events and used as the "previous" state
SNAP_FIELDS = ("status", "resale_available", "from_price",
               "onsale_utc", "presale_utc")


# ── supabase ──────────────────────────────────────────────────────────────────

def sb_client():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        log.error("SUPABASE_URL/KEY not set")
        return None
    return create_client(url, key)


# ── fetch + parse (same logic as local axs_monitor) ───────────────────────────

def fetch_event(url: str, idx: int) -> dict | None:
    for attempt in (1, 2):
        try:
            r = creq.get(url, impersonate=IMPERSONATE, timeout=30,
                         allow_redirects=True)
            if r.status_code == 200:
                return parse_event_page(url, r.text, idx)
            log.warning("event %d: HTTP %s (attempt %d)", idx, r.status_code, attempt)
        except Exception as e:
            log.warning("event %d: fetch error %s (attempt %d)",
                        idx, type(e).__name__, attempt)
        time.sleep(5)
    return None


def _price_to_float(s) -> float | None:
    if not s:
        return None
    m = re.search(r"[\d,]+(?:\.\d{1,2})?", str(s))
    if not m:
        return None
    v = float(m.group(0).replace(",", ""))
    return v if v > 0 else None


def parse_event_page(url: str, html: str, idx: int) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        log.warning("event %d: no __NEXT_DATA__ — layout changed?", idx)
        return None
    try:
        pp = json.loads(m.group(1))["props"]["pageProps"]
        ev = pp["discoveryEventData"]
    except (KeyError, json.JSONDecodeError) as e:
        log.warning("event %d: unexpected __NEXT_DATA__ shape: %s", idx, e)
        return None
    tk = pp.get("ticketingEventData") or ev.get("ticketing") or {}

    name = (ev.get("title") or {}).get("headlinersText") or \
           (pp.get("pageNameInfo") or {}).get("eventName") or url

    resale = bool(ev.get("axsMarketplaceEvent")) or bool(re.search(
        r"PURCHASE_CARDS_MODULE:AXS Official Resale|Select Tickets - AXS Official Resale",
        html, re.I))
    resale_promo_url = next(
        (p.get("url") for p in (pp.get("eventPromos") or [])
         if "resale" in (p.get("groupName") or "").lower()), None) if resale else None

    from_price = None
    for cand in (tk.get("priceLow"), tk.get("priceRange"), ev.get("ticketPriceLow"),
                 ev.get("ticketPrice"), ev.get("doorPriceLow")):
        from_price = _price_to_float(cand)
        if from_price is not None:
            break
    if from_price is None:
        m2 = re.search(r"[Ff]rom\s*\$\s*([\d,]+(?:\.\d{2})?)", html)
        if m2:
            from_price = _price_to_float(m2.group(1))

    return {
        "url": url,
        "name": name,
        "status": tk.get("status") or "",
        "onsale_utc": ev.get("onsaleDatetimeUTC"),
        "presale_utc": ev.get("presaleDatetimeUTC"),
        "event_datetime": ev.get("eventDatetimeISO") or ev.get("eventDatetime"),
        "resale_available": resale,
        "resale_url": resale_promo_url,
        "from_price": from_price,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ── change detection (identical rules to local) ───────────────────────────────

def is_buyable(snap: dict) -> bool:
    return (snap.get("status") or "").strip().lower() in BUYABLE


def diff_alerts(prev: dict | None, cur: dict, threshold: float | None) -> list[str]:
    alerts = []
    if prev is None:
        return alerts  # first sighting = baseline only

    p_prev = _price_to_float(prev.get("from_price"))
    p_cur = cur.get("from_price")
    cheap = threshold is not None and p_cur is not None and p_cur <= threshold
    where = "official resale" if cur.get("resale_available") else "primary"

    if not is_buyable(prev) and is_buyable(cur):
        tag = f" — from ${p_cur:.2f}" if p_cur is not None else ""
        if cheap:
            tag += f", AT/BELOW your ${threshold:.2f} threshold"
        alerts.append(f"BACK ON SALE: status "
                      f"'{prev.get('status') or 'unknown'}' -> '{cur['status']}'{tag}")
    if not prev.get("resale_available") and cur.get("resale_available"):
        tag = f" — from ${p_cur:.2f}" if p_cur is not None else ""
        if cheap:
            tag += f" (below your ${threshold:.2f} threshold — undercut/flip check!)"
        alerts.append(f"OFFICIAL RESALE now available on the event page{tag}")

    if p_prev is not None and p_cur is not None \
            and p_cur < p_prev - max(2.0, 0.05 * p_prev):
        tag = f" — now at/below your ${threshold:.2f} threshold" if cheap else ""
        alerts.append(f"PRICE DROP ({where}): from ${p_prev:.2f} -> ${p_cur:.2f}{tag}")

    if cheap and (p_prev is None or p_prev > threshold):
        alerts.append(f"CHEAP TICKETS ({where}): from-price ${p_cur:.2f} is "
                      f"at/below your ${threshold:.2f} threshold")

    for field, label in (("onsale_utc", "On-sale"), ("presale_utc", "Presale")):
        if (prev.get(field) or None) != (cur.get(field) or None):
            alerts.append(f"{label} time changed: {prev.get(field) or 'none'} "
                          f"-> {cur.get(field) or 'none'} (UTC)")
    return alerts


# ── auto-thresholds from Lysted list prices ───────────────────────────────────

def auto_thresholds(sb) -> dict:
    """label -> min positive Lysted list price ('{event_name} {YYYY-MM-DD}')."""
    try:
        rows = (sb.table("lysted_listings")
                .select("event_name,event_date,list_price").execute().data or [])
    except Exception as e:
        log.warning("lysted_listings read failed — no auto-thresholds: %s", e)
        return {}
    best = {}
    for r in rows:
        price = float(r.get("list_price") or 0)
        if price <= 0:
            continue
        key = f"{(r.get('event_name') or '').strip()} {str(r.get('event_date') or '')[:10]}"
        if key not in best or price < best[key]:
            best[key] = price
    return best


# ── email ─────────────────────────────────────────────────────────────────────

def send_alert_email(snap: dict, alerts: list[str]) -> bool:
    user = os.environ.get("GMAIL_USER", "")
    pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    to = os.environ.get("ALERT_TO", "") or user
    if not user or not pw or "PUT-YOUR" in pw or "PASTE-" in pw:
        log.warning("email creds not set — alert logged only")
        return False

    subject = f"AXS alert - {snap['name']}: {alerts[0]}"
    lines = [snap["name"], snap["url"], "",
             *(f"* {a}" for a in alerts), "",
             f"Status: {snap['status'] or 'unknown'}",
             f"Official resale on page: {'yes' if snap['resale_available'] else 'no'}",
             f"From-price: {'$%.2f' % snap['from_price'] if snap['from_price'] else 'not shown'}",
             f"On-sale (UTC): {snap['onsale_utc'] or 'n/a'}",
             f"Presale (UTC): {snap['presale_utc'] or 'n/a'}",
             f"Event: {snap['event_datetime'] or 'n/a'}"]
    items = "".join(f"<li>{a}</li>" for a in alerts)
    html = (f"<div style='font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#111'>"
            f"<h2 style='margin:0 0 4px'>{snap['name']}</h2>"
            f"<p style='margin:0 0 12px'><a href='{snap['url']}'>{snap['url']}</a></p>"
            f"<ul style='font-size:15px'>{items}</ul>"
            f"<p style='color:#666;font-size:13px'>Status: {snap['status'] or 'unknown'} &middot; "
            f"Resale: {'yes' if snap['resale_available'] else 'no'} &middot; "
            f"From: {'$%.2f' % snap['from_price'] if snap['from_price'] else 'not shown'}</p>"
            f"<p style='color:#999;font-size:12px'>axs_monitor (cloud)</p></div>")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText("\n".join(lines), "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.sendmail(user, [to], msg.as_string())
        return True
    except Exception as e:
        log.error("email send failed: %s", type(e).__name__)
        return False


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    sb = sb_client()
    if sb is None:
        return 1
    try:
        rows = sb.table("axs_events").select("*").execute().data or []
    except Exception as e:
        log.error("axs_events read failed: %s", e)
        return 1
    if not rows:
        log.info("no watched events in axs_events — nothing to do")
        return 0

    auto = auto_thresholds(sb)
    ok = blocked = 0
    for i, row in enumerate(rows, 1):
        threshold = row.get("cheap_threshold")
        if threshold is None:
            threshold = auto.get((row.get("label") or "").strip())
        threshold = float(threshold) if threshold is not None else None

        snap = fetch_event(row["url"], i)
        if snap is None:
            blocked += 1
            log.warning("event %d/%d: SKIP — fetch failed, keeping previous state",
                        i, len(rows))
            continue
        ok += 1

        # rows synced by the local watch-list sync carry no snapshot yet
        prev = row if row.get("checked_at") else None
        alerts = diff_alerts(prev, snap, threshold)
        fired = bool(alerts) and send_alert_email(snap, alerts)

        update = {
            "name": snap["name"],
            "status": snap["status"],
            "buyable": is_buyable(snap),
            "resale_available": snap["resale_available"],
            "resale_url": snap["resale_url"],
            "from_price": snap["from_price"],
            "cheap_threshold": threshold,
            "onsale_utc": snap["onsale_utc"],
            "presale_utc": snap["presale_utc"],
            "event_datetime": snap["event_datetime"],
            "checked_at": snap["checked_at"],
        }
        if alerts:
            update["last_alerts"] = alerts
            update["last_alert_at"] = snap["checked_at"]
        try:
            sb.table("axs_events").update(update).eq("url", row["url"]).execute()
        except Exception as e:
            log.warning("event %d: supabase update failed: %s", i, e)

        log.info("event %d/%d: ok | alerts=%d | emailed=%s",
                 i, len(rows), len(alerts), "yes" if fired else "no")
        if i < len(rows):
            time.sleep(FETCH_PAUSE_S)

    log.info("pass done: %d ok, %d failed of %d", ok, blocked, len(rows))
    # non-zero when everything failed => likely datacenter-IP block, surface it
    return 1 if rows and ok == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
