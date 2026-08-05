"""
cv_hot_report.py
-----------------------------------------------------------
Daily "Hot Board" email: the upcoming events actually trading, ranked by

    5 * sales_in_CV_HOT_DAYS  +  looking_to_go  +  0.5 * looking_to_sell

Sales come from cv_sales_seen (the best public volume proxy — it
undercounts back-to-back same-price sales but nothing better is exposed).
Each row shows the current low plus the 7d and 30d lows from cv_history,
and a "<< LOW NOW" marker when today IS the 30-day low — that marker is
the whole point of the report.

Stateless: every run rebuilds the board from the database, so it can run
anywhere without carrying anything between passes.

Env knobs:
    CV_HOT_TOP=15    how many events to list
    CV_HOT_DAYS=14   sales look-back window
    plus GMAIL_USER / GMAIL_APP_PASSWORD / ALERT_TO (see cv_email.py)

USAGE:
    python cv_hot_report.py          # build + email
    python cv_hot_report.py --dry    # print the board, no email
-----------------------------------------------------------
"""

import sys
from datetime import datetime, timedelta, timezone

import cv_email
import cv_log
import supabase_helper
from cv_drop_alert import _env_float, recent_sales
from cv_regions import region_of

log = cv_log.setup(__name__)


def volume_score(row: dict, n_sales: int) -> float:
    return (5.0 * n_sales
            + float(row.get("looking_to_go") or 0)
            + 0.5 * float(row.get("looking_to_sell") or 0))


def history_lows(sb, slugs: list) -> dict:
    """slug -> {'low7': x, 'low30': y}, the minimum low over each window."""
    if not slugs:
        return {}
    cut30 = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cut7 = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        rows = (sb.table("cv_history").select("slug,low_price,captured_at")
                .in_("slug", slugs).gte("captured_at", cut30)
                .execute().data or [])
    except Exception as e:
        log.warning("cv_history read failed: %s", e)
        return {}
    out: dict = {}
    for r in rows:
        low = float(r.get("low_price") or 0)
        if low <= 0:
            continue
        d = out.setdefault(r["slug"], {"low7": None, "low30": None})
        d["low30"] = low if d["low30"] is None else min(d["low30"], low)
        if (r.get("captured_at") or "") >= cut7:
            d["low7"] = low if d["low7"] is None else min(d["low7"], low)
    return out


def build_board(sb, top_n: int, hot_days: float) -> list:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = (sb.table("cv_prices")
            .select("slug,event_name,venue,low_price,low_price_all_in,last_sale,"
                    "looking_to_go,looking_to_sell,ask_count,ask_qty,"
                    "event_end,url")
            .execute().data or [])
    sales = recent_sales(sb, hot_days)
    board = []
    for r in rows:
        if (r.get("event_end") or "9999")[:10] < today:
            continue
        n = sales.get(r["slug"], 0)
        score = volume_score(r, n)
        if score <= 0:
            continue
        board.append({**r, "n_sales": n, "score": score})
    board.sort(key=lambda r: -r["score"])
    board = board[:top_n]
    lows = history_lows(sb, [b["slug"] for b in board])
    for b in board:
        b.update(lows.get(b["slug"], {"low7": None, "low30": None}))
    return board


def render(board: list, hot_days: float) -> tuple:
    n_with_sales = sum(1 for b in board if b["n_sales"])
    subject = (f"CrowdVolt Hot Board — top {len(board)} by volume, "
               f"{n_with_sales} with sales in {hot_days:.0f}d")
    lines, items = [], []
    for i, b in enumerate(board, 1):
        low = float(b.get("low_price") or 0)
        allin = float(b.get("low_price_all_in") or 0)
        low30 = b.get("low30")
        at_low = low30 is not None and low > 0 and low <= low30
        bits = [f"{b['n_sales']} sale(s)/{hot_days:.0f}d",
                f"{int(b.get('looking_to_go') or 0)} want in",
                f"{int(b.get('looking_to_sell') or 0)} selling",
                f"{int(b.get('ask_qty') or 0)} tickets listed"]
        if b.get("last_sale"):
            bits.append(f"last sale ${float(b['last_sale']):.0f}")
        hist = []
        if b.get("low7") is not None:
            hist.append(f"7d low ${b['low7']:.0f}")
        if low30 is not None:
            hist.append(f"30d low ${low30:.0f}")
        hists = "  (" + ", ".join(hist) + ")" if hist else ""
        marker = "  << LOW NOW" if at_low else ""
        city = region_of(b["slug"]).replace("-", " ").upper()
        lines += [f"{i:2d}. [{city}] {b.get('event_name') or b['slug']} — "
                  f"{(b.get('event_end') or '')[:10]}",
                  f"    low ${low:.0f}" + (f" (${allin:.0f} all-in)" if allin else "")
                  + hists + marker,
                  f"    {' · '.join(bits)}",
                  f"    {b.get('url') or ''}", ""]
        mark_html = ("<span style='color:#0a0;font-weight:600'> &laquo; LOW NOW</span>"
                     if at_low else "")
        items.append(
            f"<li style='margin-bottom:12px'><b>{i}. </b>"
            f"<span style='background:#eee;border-radius:3px;padding:1px 5px;"
            f"font-size:11px;letter-spacing:.5px'>{city}</span> "
            f"<b>{b.get('event_name') or b['slug']}</b> — {(b.get('event_end') or '')[:10]}"
            f"<br>low <b>${low:.0f}</b>"
            + (f" (${allin:.0f} all-in)" if allin else "") + hists + mark_html
            + f"<br><span style='color:#666;font-size:13px'>{' · '.join(bits)}</span>"
            f"<br><a href='{b.get('url') or ''}'>{b.get('url') or ''}</a></li>")
    text = "\n".join(lines)
    html = ("<div style='font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#111'>"
            "<h2 style='margin:0 0 10px'>CrowdVolt Hot Board</h2>"
            "<p style='margin:0 0 12px;color:#444'>Best-selling / highest-volume "
            "upcoming events, ranked by sales seen + demand. Lows from cv_history.</p>"
            f"<ul style='font-size:15px;list-style:none;padding:0'>{''.join(items)}</ul>"
            "<p style='color:#999;font-size:12px'>daily</p></div>")
    return subject, text, html


def run(dry: bool = False) -> int:
    top_n = int(_env_float("CV_HOT_TOP", 15))
    hot_days = _env_float("CV_HOT_DAYS", 14.0)

    sb = supabase_helper.client()
    if sb is None:
        log.error("no Supabase client — SUPABASE_URL/SUPABASE_KEY not set")
        return 1

    # The board reports "current low" per event. If the scan has stopped, those
    # are yesterday's lows wearing today's date — the 2026-08-05 board went out
    # on prices frozen 21h earlier. A daily board can tolerate a longer window
    # than the sniper, but not an unbounded one.
    stale, why = supabase_helper.feed_is_stale(sb, default_max_min=720.0)
    if stale and not dry:
        log.error("NOT sending the board: %s", why)
        return 1
    log.info("feed check: %s", why)

    board = build_board(sb, top_n, hot_days)
    if not board:
        log.info("no upcoming events with any volume signal — no board today")
        return 0

    subject, text, html = render(board, hot_days)
    if dry:
        print(f"\n--- DRY HOT BOARD ---\nSUBJECT: {subject}\n\n{text}---------------")
        return 0

    if cv_email.send(subject, text, html):
        log.info("hot board emailed: %d event(s), %d at their 30d low",
                 len(board), sum(1 for b in board
                                 if b.get("low30") is not None
                                 and float(b.get("low_price") or 0) > 0
                                 and float(b["low_price"]) <= b["low30"]))
    else:
        # Deliberately NOT dumping the board text here — it carries event
        # names and URLs, and these logs are public.
        log.warning("email not configured — %d event(s) not delivered", len(board))
    return 0


if __name__ == "__main__":
    sys.exit(run(dry="--dry" in sys.argv))
