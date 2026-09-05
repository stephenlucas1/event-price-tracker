"""
cv_undercut_alert.py
-----------------------------------------------------------
Alert when someone lists UNDER one of your own CrowdVolt asks.

Your asks live in `cv_my_listings` (slug, my_price = the seller price Ion
shows, qty). After every successful cv_scan.py pass this compares each
active listing against the event's fresh lowest ask (`cv_prices.low_price`,
also a seller price, so no fee math is needed) and classifies it:

    lowest       my_price <  low  is impossible; my_price == low and the
                 ladder has one price point at the bottom -> you own the floor
    tied         my_price == low but the bottom price point carries more
                 qty than yours -> someone matched you
    undercut     low < my_price -> someone is under you
    not_visible  low > my_price -> your ask isn't in the book (sold out,
                 delisted, or the price you typed is wrong)

Alerts fire on TRANSITIONS only — lowest/tied -> undercut is the one that
matters; undercut -> lowest ("back on top") and lowest -> not_visible
("did it sell?") are sent as quieter info lines in the same digest. A
listing first seen already undercut alerts once, because that is the
actionable case. Then nothing repeats until the standing changes again.

State is `cv_undercut_state` in the database for the same reason
cv_drop_alert keeps its state there: runner disk is wiped every chain.

Each digest goes out as ONE email plus dice_alerts rows (kind
"CrowdVolt Undercut") and ONE web push, exactly like cv_drop_alert.

Env knobs:
    CV_UNDERCUT_TOL=0.5   dollars of slack before "under" counts as under
    plus GMAIL_* / ALERT_TO / VAPID_* (see cv_email.py, notify_helper.py)

USAGE:
    python cv_undercut_alert.py           # one pass
    python cv_undercut_alert.py --dry     # print, no email/push/state
    python cv_undercut_alert.py --status  # table of every active listing
-----------------------------------------------------------
"""

import html
import os
import sys
from datetime import datetime, timezone

import cv_email
import cv_log
import notify_helper
import supabase_helper
from cv_regions import region_of

log = cv_log.setup(__name__)

LISTINGS_TABLE = "cv_my_listings"
STATE_TABLE = "cv_undercut_state"
KIND = "CrowdVolt Undercut"


def _tol() -> float:
    try:
        return float(os.environ.get("CV_UNDERCUT_TOL") or 0.5)
    except ValueError:
        return 0.5


# ------------------------------------------------------------------ reads

def load_listings(sb) -> list:
    try:
        return (sb.table(LISTINGS_TABLE).select("*")
                .eq("active", True).execute().data or [])
    except Exception as e:
        log.error("%s read failed: %s", LISTINGS_TABLE, e)
        return []


def load_prices(sb, slugs: list) -> dict:
    if not slugs:
        return {}
    try:
        rows = (sb.table("cv_prices")
                .select("slug,event_name,venue,low_price,low_price_all_in,"
                        "asks,ask_count,ask_qty,event_end,url,updated_at")
                .in_("slug", slugs).execute().data or [])
    except Exception as e:
        log.error("cv_prices read failed: %s", e)
        return {}
    return {r["slug"]: r for r in rows}


def load_state(sb) -> dict:
    try:
        rows = sb.table(STATE_TABLE).select("slug,status,low,my_price").execute().data or []
    except Exception as e:
        log.error("%s read failed: %s", STATE_TABLE, e)
        return {}
    return {r["slug"]: r for r in rows}


def save_state(sb, rows: list, gone: list):
    now = datetime.now(timezone.utc).isoformat()
    if rows:
        try:
            sb.table(STATE_TABLE).upsert(
                [dict(r, updated_at=now) for r in rows], on_conflict="slug").execute()
        except Exception as e:
            log.error("%s upsert failed: %s", STATE_TABLE, e)
    for slug in gone:
        try:
            sb.table(STATE_TABLE).delete().eq("slug", slug).execute()
        except Exception as e:
            log.error("%s delete failed for %s: %s", STATE_TABLE, cv_log.event_id(slug), e)


def retire_listing(sb, slug: str):
    """Event is over — switch the listing off so it stops being evaluated."""
    try:
        sb.table(LISTINGS_TABLE).update({"active": False}).eq("slug", slug).execute()
    except Exception as e:
        log.error("retire failed for %s: %s", cv_log.event_id(slug), e)


# --------------------------------------------------------------- classify

def _is_past(end_iso: str) -> bool:
    if not end_iso:
        return False
    try:
        from dateutil import parser as dp
        t = dp.parse(end_iso)
        if t.tzinfo is None:            # a bare date compares as UTC
            t = t.replace(tzinfo=timezone.utc)
        return t < datetime.now(timezone.utc)
    except Exception:
        return False


def classify(listing: dict, price: dict, tol: float) -> dict:
    """Standing of one listing against the fresh book. Pure; no I/O."""
    my = float(listing["my_price"])
    my_qty = int(listing.get("qty") or 1)
    low = float(price.get("low_price") or 0)
    all_in = float(price.get("low_price_all_in") or 0)
    ladder = price.get("asks") or []
    # All-in <-> seller ratio for THIS event (CrowdVolt's buyer fee rounds
    # oddly, so use the event's own ratio rather than a constant).
    ratio = (all_in / low) if (low > 0 and all_in > 0) else 1.0
    my_all_in = my * ratio

    if low <= 0:
        status = "not_visible"          # book is empty
    elif low < my - tol:
        status = "undercut"
    elif low > my + tol:
        status = "not_visible"
    else:
        bottom_qty = int(ladder[0]["q"]) if ladder else my_qty
        status = "tied" if bottom_qty > my_qty else "lowest"

    below = [a for a in ladder if float(a["p"]) < my_all_in - tol]
    above = [a for a in ladder if float(a["p"]) > my_all_in + tol]
    return {
        "slug": listing["slug"],
        "event_name": html.unescape(price.get("event_name") or listing.get("event_name") or listing["slug"]),
        "venue": price.get("venue") or "",
        "event_end": (price.get("event_end") or "")[:10],
        "url": price.get("url") or f"https://www.crowdvolt.com/event/{listing['slug']}",
        "status": status,
        "my_price": my, "my_qty": my_qty, "my_all_in": round(my_all_in),
        "low": low, "all_in": all_in,
        "n_below": len(below), "qty_below": sum(int(a["q"]) for a in below),
        "next_above": (float(above[0]["p"]) / ratio) if above else None,
        "suggest": max(1, round(low - 1)) if status == "undercut" and low > 1 else None,
        "ask_count": int(price.get("ask_count") or 0),
        "updated_at": price.get("updated_at") or "",
    }


def transitions(cur: dict, prev: dict | None) -> str | None:
    """Which alert, if any, this pass's standing earns. None = stay quiet."""
    s = cur["status"]
    p = (prev or {}).get("status")
    if s == "undercut" and p != "undercut":
        return "undercut"                       # incl. first sighting already under
    if prev is None:
        return None                             # baseline, nothing else to say
    if s in ("lowest", "tied") and p == "undercut":
        return "back_on_top"
    if s == "not_visible" and p in ("lowest", "tied"):
        return "vanished"
    if s == "tied" and p == "lowest":
        return "matched"
    return None


# ----------------------------------------------------------------- output

_LABEL = {
    "undercut": "UNDERCUT",
    "matched": "matched",
    "back_on_top": "back on top",
    "vanished": "listing gone — sold?",
}


def _line(a: dict) -> str:
    c = a["cur"]
    if a["kind"] == "undercut":
        s = f"low ${c['low']:.0f} vs your ${c['my_price']:.0f}"
        if c["n_below"]:        # ladder is empty until the scanner stores it
            s += f" — {c['n_below']} ask(s) / {c['qty_below']} tix under you"
        if c["suggest"]:
            s += f" · reprice ${c['suggest']}"
        return s
    if a["kind"] == "matched":
        return f"someone matched your ${c['my_price']:.0f}"
    if a["kind"] == "back_on_top":
        s = f"you're lowest again at ${c['my_price']:.0f}"
        if c["next_above"]:
            s += f" · next ask ${c['next_above']:.0f}"
        return s
    return f"lowest ask is now ${c['low']:.0f} — your ${c['my_price']:.0f} isn't in the book"


def render_digest(alerts: list) -> tuple[str, str, str]:
    under = [a for a in alerts if a["kind"] == "undercut"]
    lead = (under or alerts)[0]["cur"]
    n = len(alerts)
    if under:
        subject = (f"CrowdVolt UNDERCUT - {lead['event_name']}: "
                   f"${lead['low']:.0f} under your ${lead['my_price']:.0f}")
    else:
        subject = f"CrowdVolt listing update - {lead['event_name']}: {_LABEL[alerts[0]['kind']]}"
    if n > 1:
        subject += f" +{n-1} more"

    lines, items = [], []
    for a in alerts:
        c = a["cur"]
        city = region_of(c["slug"]).replace("-", " ").upper()
        allin = f" (${c['all_in']:.0f} all-in)" if c["all_in"] else ""
        lines += [f"* [{city}] {c['event_name']} — {c['event_end']}  [{_LABEL[a['kind']]}]",
                  f"  {_line(a)}{allin}",
                  f"  {c['url']}", ""]
        colour = "#c00" if a["kind"] == "undercut" else "#555"
        items.append(
            f"<li style='margin-bottom:10px'>"
            f"<span style='background:#eee;border-radius:3px;padding:1px 5px;"
            f"font-size:11px;letter-spacing:.5px'>{city}</span> "
            f"<b>{html.escape(c['event_name'])}</b> — {c['event_end']} "
            f"<span style='color:{colour};font-weight:700'>{_LABEL[a['kind']]}</span>"
            f"<br>{html.escape(_line(a))}{allin}"
            f"<br><a href='{c['url']}'>{c['url']}</a></li>")
    text = "\n".join(lines)
    body = ("<div style='font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#111'>"
            "<h2 style='margin:0 0 10px'>Your CrowdVolt listings</h2>"
            f"<ul style='font-size:15px;list-style:none;padding:0'>{''.join(items)}</ul>"
            "<p style='color:#999;font-size:12px'>your ask vs the live book, "
            "checked after every scan (~15 min)</p></div>")
    return subject, text, body


def post_to_feed(sb, alerts: list, subject: str) -> None:
    """Same alerts into the app's Alerts tab + one push. Mirrors
    cv_drop_alert.post_to_feed; event_id prefixed "cv:" so it never collides
    with a Dice id."""
    rows = []
    for a in alerts:
        c = a["cur"]
        city = region_of(c["slug"]).replace("-", " ").upper()
        rows.append({
            "event_id": "cv:" + c["slug"],
            "event_name": c["event_name"],
            "venue": " · ".join(x for x in (c["venue"], city, c["event_end"]) if x),
            "kind": KIND,
            "summary": f"{_LABEL[a['kind']]}: {_line(a)}",
            "detail": {"change": a["kind"], "status": c["status"],
                       "my_price": c["my_price"], "my_qty": c["my_qty"],
                       "low": c["low"], "all_in": c["all_in"],
                       "n_below": c["n_below"], "qty_below": c["qty_below"],
                       "next_above": c["next_above"], "suggest": c["suggest"]},
            "url": c["url"], "cv_url": c["url"],
        })
    first_id = None
    try:
        ins = sb.table("dice_alerts").insert(rows).execute()
        ids = [r.get("id") for r in (ins.data or []) if r.get("id") is not None]
        first_id = min(ids) if ids else None
    except Exception as e:
        log.error("dice_alerts insert failed: %s", type(e).__name__)
        return
    body = "\n".join(f"{r['event_name']}: {r['summary']}" for r in rows)[:180]
    app_url = f"/?tab=alerts&alert={first_id}" if first_id else "/?tab=alerts"
    icon = "🔻" if any(a["kind"] == "undercut" for a in alerts) else "💜"
    try:
        n = notify_helper.send_push(f"{icon} " + subject, body, app_url)
    except Exception as e:
        log.error("push failed: %s", type(e).__name__)
        n = 0
    if first_id is not None and n:
        try:
            (sb.table("dice_alerts").update({"pushed": True})
             .eq("kind", KIND).gte("id", first_id).execute())
        except Exception:
            pass
    log.info("feed: %d row(s), push to %d device(s)", len(rows), n)


# -------------------------------------------------------------------- run

def evaluate(listings: list, prices: dict, state: dict, tol: float):
    """Returns (alerts, new_state_rows, gone_slugs, retired_slugs)."""
    alerts, new_rows, gone, retired = [], [], [], []
    for li in listings:
        slug = li["slug"]
        pr = prices.get(slug)
        if pr is None:
            log.warning("%s: no cv_prices row yet (not scanned, or slug typo)",
                        cv_log.event_id(slug))
            continue
        if _is_past(pr.get("event_end") or ""):
            retired.append(slug)
            if slug in state:
                gone.append(slug)
            continue
        cur = classify(li, pr, tol)
        prev = state.get(slug)
        kind = transitions(cur, prev)
        if kind:
            alerts.append({"kind": kind, "cur": cur})
        if (prev is None or prev.get("status") != cur["status"]
                or float(prev.get("low") or 0) != cur["low"]
                or float(prev.get("my_price") or 0) != cur["my_price"]):
            new_rows.append({"slug": slug, "status": cur["status"],
                             "low": cur["low"], "my_price": cur["my_price"]})
    # State rows whose listing was deactivated in the app: forget them, so a
    # relist later starts from a clean baseline.
    active = {li["slug"] for li in listings}
    gone += [s for s in state if s not in active and s not in gone]
    return alerts, new_rows, gone, retired


def print_status(listings: list, prices: dict, tol: float):
    print(f"{'standing':12} {'mine':>6} {'low':>6} {'below':>5}  event")
    for li in listings:
        pr = prices.get(li["slug"])
        if not pr:
            print(f"{'(unscanned)':12} {float(li['my_price']):6.0f} {'-':>6} {'-':>5}  {li['slug']}")
            continue
        c = classify(li, pr, tol)
        print(f"{c['status']:12} {c['my_price']:6.0f} {c['low']:6.0f} {c['n_below']:5d}  "
              f"{c['event_name']} ({c['event_end']})")


def run(dry: bool = False, status_only: bool = False) -> int:
    tol = _tol()
    sb = supabase_helper.client()
    if sb is None:
        log.error("no Supabase client — SUPABASE_URL/SUPABASE_KEY not set")
        return 1

    listings = load_listings(sb)
    if not listings:
        log.info("no active listings in %s — nothing to watch", LISTINGS_TABLE)
        return 0
    prices = load_prices(sb, [li["slug"] for li in listings])

    if status_only:
        print_status(listings, prices, tol)
        return 0

    # Same guard as the drop sniper: a frozen feed must not read as "safe".
    stale, why = supabase_helper.feed_is_stale(sb)
    if stale and not dry:
        log.error("NOT evaluating: %s", why)
        return 1
    log.info("feed check: %s", why)

    state = load_state(sb)
    alerts, new_rows, gone, retired = evaluate(listings, prices, state, tol)

    if not dry:
        save_state(sb, new_rows, gone)
        for slug in retired:
            retire_listing(sb, slug)
    if retired:
        log.info("retired %d listing(s) whose event has passed", len(retired))

    if not alerts:
        log.info("no change among %d listing(s)", len(listings))
        return 0

    subject, text, body = render_digest(alerts)
    if dry:
        print(f"\n--- DRY DIGEST ---\nSUBJECT: {subject}\n{text}------------------")
        sent = True
    else:
        sent = cv_email.send(subject, text, body)
        post_to_feed(sb, alerts, subject)

    for a in alerts:
        c = a["cur"]
        log.info("%-12s %s mine $%.0f low $%.0f %s", a["kind"].upper(),
                 cv_log.event_id(c["slug"]), c["my_price"], c["low"],
                 "emailed" if sent else "LOGGED ONLY")
    return 0


if __name__ == "__main__":
    sys.exit(run(dry="--dry" in sys.argv, status_only="--status" in sys.argv))
