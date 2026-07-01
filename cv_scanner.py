"""
cv_scanner.py
─────────────────────────────────────────────────────────────
CrowdVolt price tracker. Reads public event pages, stores the
lowest ask per event in Supabase, and sends a Web Push alert
when a price drops notably or a sold-out event comes back.

No login, no CrowdVolt account, no API key needed.

Config (GitHub Actions vars / env):
  CV_CITIES      Comma list of city keywords to scope slugs
                 e.g. "new-york,brooklyn,miami". Empty = all events.
  CV_MAX_EVENTS  Max event pages per run (default 300)
  CV_DROP_PCT    Min % drop to alert on (default 15)
  CV_DELAY       Seconds between requests, jittered (default 0.8)
─────────────────────────────────────────────────────────────
"""

import os
import re
import sys
import time
import random
import logging
from datetime import datetime, timezone

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# This runs in a public repo, so Actions logs are world-readable. Keep HTTP
# client chatter (full request URLs) out of them.
for _n in ("httpx", "httpcore", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)

BASE     = "https://www.crowdvolt.com"
SITEMAP  = f"{BASE}/sitemap.xml"
EVENT_RE = re.compile(r"https://www\.crowdvolt\.com/event/[a-z0-9-]+")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

CITIES     = [c.strip().lower() for c in os.environ.get("CV_CITIES", "").split(",") if c.strip()]
MAX_EVENTS = int(os.environ.get("CV_MAX_EVENTS", "300"))
DROP_PCT   = float(os.environ.get("CV_DROP_PCT", "15"))
REQ_DELAY  = float(os.environ.get("CV_DELAY", "0.8"))
MAX_PUSH   = 6


def _headers():
    return {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": BASE + "/",
    }


def _get(url: str) -> str:
    r = requests.get(url, headers=_headers(), timeout=25)
    r.raise_for_status()
    return r.text


def _field(pat: str, html: str):
    m = re.search(pat, html)
    return m.group(1) if m else None


def parse_event(html: str) -> dict:
    low = _field(r'\\?"lowPrice\\?":\\?"?([0-9]+(?:\.[0-9]+)?)', html)
    if low is None:
        return {}
    high  = _field(r'\\?"highPrice\\?":\\?"?([0-9]+(?:\.[0-9]+)?)', html)
    avail = _field(r'\\?"availability\\?":\\?"https://schema\.org/([A-Za-z]+)', html)
    end   = _field(r'\\?"validThrough\\?":\\?"([0-9T:+\-]+)', html)
    venue = _field(r'\\?"venue\\?":\\?"([^"\\]{2,90})', html)
    title = _field(r'og:title\\?"[^>]*content=\\?"([^"\\]{2,120})', html) or ""
    name  = re.split(r'\s+tickets\s+-\s+', title, maxsplit=1)[0].strip()
    return {
        "low_price":    float(low),
        "high_price":   float(high) if high else float(low),
        "availability": avail or "",
        "event_end":    end or "",
        "event_name":   name.strip(),
        "venue":        (venue or "").strip(),
    }


def _slug(url: str) -> str:
    return url.rsplit("/event/", 1)[-1]


def _in_scope(url: str) -> bool:
    if not CITIES:
        return True
    s = _slug(url).lower()
    return any(c in s for c in CITIES)


def _is_past(end_iso: str) -> bool:
    if not end_iso:
        return False
    try:
        from dateutil import parser as dp
        return dp.parse(end_iso) < datetime.now(timezone.utc)
    except Exception:
        return False


def _load_prev() -> dict:
    import supabase_helper as db
    sb = db.client()
    if not sb:
        return {}
    try:
        r = sb.table("cv_prices").select("slug,low_price,availability").execute()
        return {row["slug"]: row for row in (r.data or [])}
    except Exception as e:
        log.error("load cv_prices failed: %s", e)
        return {}


def _save(slug: str, url: str, d: dict):
    import supabase_helper as db
    sb = db.client()
    if not sb:
        return
    try:
        sb.table("cv_prices").upsert({
            "slug":         slug,
            "event_name":   d["event_name"],
            "venue":        d["venue"],
            "low_price":    d["low_price"],
            "high_price":   d["high_price"],
            "availability": d["availability"],
            "event_end":    d["event_end"],
            "url":          url,
            "updated_at":   datetime.now(timezone.utc).isoformat(),
        }, on_conflict="slug").execute()
    except Exception as e:
        log.error("save cv_prices failed for %s: %s", slug, e)


def _push_deals(deals: list):
    if not deals:
        return
    try:
        import notify_helper as notify
    except Exception:
        return
    for d in deals[:MAX_PUSH]:
        notify.send_push(
            f"🔥 {d['event_name']} — ${d['low']:.0f}",
            f"Was ${d['prev']:.0f} · now ${d['low']:.0f} ({d['venue']})",
            d["url"],
        )
    if len(deals) > MAX_PUSH:
        extra = deals[MAX_PUSH:]
        lines = "\n".join(f"• {d['event_name']} ${d['low']:.0f}" for d in extra[:12])
        notify.send_push(f"🔥 {len(extra)} more price drops", lines, BASE)


def run():
    # Log the city *count*, not the list — these logs are public.
    log.info("-- scan started (%d city filter(s), cap=%d, drop>=%.0f%%) --",
             len(CITIES), MAX_EVENTS, DROP_PCT)

    try:
        sm = _get(SITEMAP)
    except Exception as e:
        log.error("Sitemap fetch failed: %s", e)
        sys.exit(1)

    urls = [u for u in dict.fromkeys(EVENT_RE.findall(sm)) if _in_scope(u)]
    log.info("Sitemap: %d in-scope event URLs", len(urls))
    if len(urls) > MAX_EVENTS:
        urls = urls[:MAX_EVENTS]

    prev = _load_prev()
    deals, scanned, skipped = [], 0, 0

    for url in urls:
        slug = _slug(url)
        try:
            d = parse_event(_get(url))
        except Exception as e:
            log.warning("skip %s: %s", slug, e)
            time.sleep(REQ_DELAY)
            continue

        if not d:
            skipped += 1
            time.sleep(REQ_DELAY * random.uniform(0.6, 1.4))
            continue
        if _is_past(d["event_end"]):
            time.sleep(REQ_DELAY * random.uniform(0.6, 1.4))
            continue

        scanned += 1
        p = prev.get(slug)
        if p and p.get("low_price"):
            old  = float(p["low_price"])
            new  = d["low_price"]
            drop = (old - new) / old * 100 if old else 0
            back = (p.get("availability") not in ("InStock", "")
                    and d["availability"] == "InStock")
            if new < old and drop >= DROP_PCT:
                deals.append({"event_name": d["event_name"], "venue": d["venue"],
                              "low": new, "prev": old, "url": url})
                log.info("DEAL %s: $%.0f → $%.0f (-%.0f%%)", slug, old, new, drop)
            elif back:
                deals.append({"event_name": d["event_name"], "venue": d["venue"],
                              "low": new, "prev": old, "url": url})
                log.info("RESTOCK %s: $%.0f", slug, new)

        _save(slug, url, d)
        time.sleep(REQ_DELAY * random.uniform(0.6, 1.4))

    _push_deals(deals)
    log.info("-- done: %d priced, %d unpriced, %d deals --", scanned, skipped, len(deals))


if __name__ == "__main__":
    run()
