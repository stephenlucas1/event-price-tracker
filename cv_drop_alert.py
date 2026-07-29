"""
cv_drop_alert.py
-----------------------------------------------------------
Email when an event's lowest ask DROPS sharply — someone just listed well
under the going rate, i.e. a snipe/flip window.

Runs right after cv_scan.py in the same cycle, so "previous" means "as of
the last scan, ~15 min ago". Alerts when:

    new_low <= previous_low * (1 - CV_DROP_PCT/100)     # default 10%
    and (previous_low >= CV_POPULAR_MIN                 # default $75
         or the event is HOT)                           # volume-ranked
    and the event hasn't happened yet

HOT = real velocity or demand: sales detected in cv_sales_seen over the
last CV_HOT_DAYS (default 14) >= CV_HOT_SALES (default 3), or
looking_to_go >= CV_HOT_DEMAND (default 20). Hot events alert even under
the price floor — a $60 low on a high-volume event is a better flip than
an $80 low on a dead one.

All drops in a pass go into ONE digest email. First sighting of an event
is baseline only. The stored low is updated every pass, up or down, so an
event that climbs back re-arms itself for the next sharp drop.

STATE LIVES IN THE DATABASE (cv_alert_state), not a local file. On a
runner a local file is wiped every time the job chain restarts, which
would silently re-baseline every event and skip a pass of alerts every
few hours. Shared state also means whichever host is driving the scan
picks up exactly where the other left off.

Env knobs:
    CV_DROP_PCT=10        alert threshold, percent drop pass-over-pass
    CV_POPULAR_MIN=75     ignore events whose reference low is under this
    CV_HOT_SALES=3        sales in CV_HOT_DAYS that make an event hot
    CV_HOT_DEMAND=20      looking_to_go count that makes an event hot
    CV_HOT_DAYS=14        sales look-back window
    plus GMAIL_USER / GMAIL_APP_PASSWORD / ALERT_TO (see cv_email.py)

USAGE:
    python cv_drop_alert.py          # one pass
    python cv_drop_alert.py --dry    # print the would-be digest, no email
-----------------------------------------------------------
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import cv_email
import cv_log
import supabase_helper
from cv_regions import region_of

log = cv_log.setup(__name__)

STATE_TABLE = "cv_alert_state"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def load_state(sb) -> dict:
    """{slug: previous_low}. Empty means first run — baseline only."""
    try:
        rows = sb.table(STATE_TABLE).select("slug,low").execute().data or []
    except Exception as e:
        log.error("cv_alert_state read failed: %s", e)
        return {}
    return {r["slug"]: float(r["low"]) for r in rows if r.get("low") is not None}


def save_state(sb, changed: dict, gone: list):
    """Write only what moved. Rewriting every row each pass would be a few
    hundred pointless writes every 15 minutes."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"slug": s, "low": v, "updated_at": now} for s, v in changed.items()]
    for i in range(0, len(rows), 100):
        try:
            sb.table(STATE_TABLE).upsert(rows[i:i + 100], on_conflict="slug").execute()
        except Exception as e:
            log.error("cv_alert_state upsert failed: %s", e)
    for slug in gone:
        try:
            sb.table(STATE_TABLE).delete().eq("slug", slug).execute()
        except Exception as e:
            log.error("cv_alert_state delete failed for %s: %s",
                      cv_log.event_id(slug), e)


def recent_sales(sb, days: float) -> dict:
    """slug -> sales detected in the last `days` (from cv_sales_seen)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        rows = (sb.table("cv_sales_seen").select("slug")
                .gte("seen_at", since).execute().data or [])
    except Exception as e:
        log.warning("cv_sales_seen read failed — hot ranking off: %s", e)
        return {}
    counts: dict = {}
    for r in rows:
        counts[r["slug"]] = counts.get(r["slug"], 0) + 1
    return counts


def find_drops(rows: list, state: dict, drop_pct: float, popular_min: float,
               sales_counts: dict | None = None,
               hot_sales: int = 3, hot_demand: int = 20):
    """Returns (drops, new_lows, finished_slugs).

    Unlike the old in-place version this does not mutate `state`; the caller
    needs the before/after difference to know which rows to write back.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sales_counts = sales_counts or {}
    drops, new_lows, finished = [], {}, []
    for r in rows:
        slug = r.get("slug")
        low = float(r.get("low_price") or 0)
        if not slug or low <= 0:
            continue
        if (r.get("event_end") or "9999")[:10] < today:
            if slug in state:
                finished.append(slug)      # event happened — forget it
            continue
        n_sales = sales_counts.get(slug, 0)
        demand = int(r.get("looking_to_go") or 0)
        hot = n_sales >= hot_sales or demand >= hot_demand
        prev = float(state.get(slug) or 0)
        if (prev > 0 and (prev >= popular_min or hot)
                and low <= prev * (1 - drop_pct / 100)):
            drops.append({
                "slug": slug,
                "event_name": r.get("event_name") or slug,
                "venue": r.get("venue") or "",
                "event_end": (r.get("event_end") or "")[:10],
                "url": r.get("url") or "",
                "prev": prev, "low": low,
                "all_in": float(r.get("low_price_all_in") or 0),
                "pct": (prev - low) / prev * 100,
                "hot": hot, "n_sales": n_sales, "demand": demand,
                "last_sale": float(r.get("last_sale") or 0),
            })
        if state.get(slug) != low:
            new_lows[slug] = low
    return drops, new_lows, finished


def render_digest(drops: list) -> tuple[str, str, str]:
    n = len(drops)
    top = max(drops, key=lambda d: d["pct"])
    subject = (f"CrowdVolt drop - {top['event_name']}: "
               f"${top['prev']:.0f} -> ${top['low']:.0f} (-{top['pct']:.0f}%)"
               + (f" +{n-1} more" if n > 1 else ""))
    lines, items = [], []
    for d in sorted(drops, key=lambda d: -d["pct"]):
        allin = f" (${d['all_in']:.0f} all-in)" if d["all_in"] else ""
        vol_bits = []
        if d.get("hot"):
            vol_bits.append("HOT")
        if d.get("n_sales"):
            vol_bits.append(f"{d['n_sales']} sale(s)/14d")
        if d.get("demand"):
            vol_bits.append(f"{d['demand']} looking to go")
        if d.get("last_sale"):
            vol_bits.append(f"last sale ${d['last_sale']:.0f}")
        vol = ("  [" + " · ".join(vol_bits) + "]") if vol_bits else ""
        # The scan covers every city, so say which one — an unlabelled $60
        # drop is useless when it could be any market.
        city = region_of(d["slug"]).replace("-", " ").upper()
        lines += [f"* [{city}] {d['event_name']} — {d['event_end']}",
                  f"  ${d['prev']:.2f} -> ${d['low']:.2f}  (-{d['pct']:.0f}%){allin}{vol}",
                  f"  {d['url']}", ""]
        vol_html = (f"<br><span style='color:#b45f06;font-size:13px'>"
                    f"{' · '.join(vol_bits)}</span>") if vol_bits else ""
        items.append(
            f"<li style='margin-bottom:10px'>"
            f"<span style='background:#eee;border-radius:3px;padding:1px 5px;"
            f"font-size:11px;letter-spacing:.5px'>{city}</span> "
            f"<b>{d['event_name']}</b> — {d['event_end']}"
            f"<br>${d['prev']:.2f} &rarr; <b>${d['low']:.2f}</b> "
            f"<span style='color:#c00'>(-{d['pct']:.0f}%)</span>{allin}{vol_html}"
            f"<br><a href='{d['url']}'>{d['url']}</a></li>")
    text = "\n".join(lines)
    html = ("<div style='font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#111'>"
            "<h2 style='margin:0 0 10px'>CrowdVolt price drops</h2>"
            f"<ul style='font-size:15px;list-style:none;padding:0'>{''.join(items)}</ul>"
            "<p style='color:#999;font-size:12px'>lowest ask vs previous scan "
            "(~15 min ago)</p></div>")
    return subject, text, html


def run(dry: bool = False) -> int:
    drop_pct = _env_float("CV_DROP_PCT", 10.0)
    popular_min = _env_float("CV_POPULAR_MIN", 75.0)
    hot_sales = int(_env_float("CV_HOT_SALES", 3))
    hot_demand = int(_env_float("CV_HOT_DEMAND", 20))
    hot_days = _env_float("CV_HOT_DAYS", 14.0)

    sb = supabase_helper.client()
    if sb is None:
        log.error("no Supabase client — SUPABASE_URL/SUPABASE_KEY not set")
        return 1

    try:
        rows = (sb.table("cv_prices")
                .select("slug,event_name,venue,low_price,low_price_all_in,"
                        "availability,event_end,url,looking_to_go,last_sale")
                .execute().data or [])
    except Exception as e:
        log.error("cv_prices read failed: %s", e)
        return 1

    sales_counts = recent_sales(sb, hot_days)
    state = load_state(sb)
    first_run = not state
    drops, new_lows, finished = find_drops(
        rows, state, drop_pct, popular_min, sales_counts, hot_sales, hot_demand)

    if not dry:
        save_state(sb, new_lows, finished)

    if first_run:
        log.info("baseline pass: %d event(s) recorded, alerts start next pass",
                 len(new_lows))
        return 0

    if not drops:
        log.info("no drops >= %.0f%% among %d tracked event(s)",
                 drop_pct, len(state))
        return 0

    subject, text, html = render_digest(drops)
    if dry:
        print(f"\n--- DRY DIGEST ---\nSUBJECT: {subject}\n{text}------------------")
        sent = True
    else:
        sent = cv_email.send(subject, text, html)

    for d in drops:
        # Redacted: these logs are public. The email carries the real names.
        log.info("DROP %-12s $%.2f -> $%.2f (-%.0f%%)%s %s",
                 cv_log.event_id(d["slug"]), d["prev"], d["low"], d["pct"],
                 " HOT" if d["hot"] else "", "emailed" if sent else "LOGGED ONLY")
    return 0


if __name__ == "__main__":
    sys.exit(run(dry="--dry" in sys.argv))
