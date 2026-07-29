"""
cv_scan.py
-----------------------------------------------------------
CrowdVolt market scan — the canonical scanner, runs anywhere.

This replaces cv_scanner.py as the thing the workflow actually runs.
cv_scanner.py stays as the low-level fetch/parse layer (and as the
schema.org-only fallback); everything above it lives here.

Why it runs here (2026-07-28): the scan used to be driven from a
workstation that gets powered off overnight, which produced a verified
43-hour gap on 2026-07-26 -> 07-27 — every stored low frozen for most of
two days. Nothing in this scan needs a residential IP; CrowdVolt is
public and ungated, so the runner is a strictly better host. The
workstation keeps a copy as a hot spare that stands by while this one is
alive, so there is normally exactly one writer.

What this does per pass, over what cv_scanner.py did:
  * writes low_price_all_in (the fee-inclusive price a buyer actually
    pays), parsed from the page's RSC payload — schema.org carries only
    the base ask;
  * captures the demand/supply signals the same fetch already returns:
    looking_to_go / looking_to_sell, event-level last_sale, max_bid,
    tickets_remaining, and the ask book's depth. A change in any tracked
    signal appends a cv_history row (lows over time without table bloat);
    a changed last_sale inserts a cv_sales_seen row (the sales-velocity
    feed the downstream reports and alerts rank on);
  * scans every region, not just one city, via cv_regions.py — slugs spell
    the same market a dozen ways ("-nyc", "montauk", "buschwick", bare
    venue names), so a raw substring filter silently dropped real events;
  * prunes cv_prices rows that fall out of the sitemap OR out of scope.
    An unrefreshed row is a zombie by construction: it keeps a frozen
    price and downstream consumers still read it as current.

The pass is BUDGETED, not truncated. Scanning the whole sitemap at ~1.15s
an event takes ~10 min, so instead of capping the event list (which loses
the same tail every pass) the loop spends CV_BUDGET_S and orders the queue
so a spent budget costs the least:
    1. home region, already tracked  (stalest first)
    2. home region, never seen
    3. other regions, already tracked (stalest first)
    4. other regions, never seen
Tracked-before-new is load-bearing: about half the sitemap is real events
with zero asks/bids/watchers, never-seen correlates with empty, and the
drop sniper can only compare against events that already have a price. On
a 60s test budget the naive "new first" order scanned 57 events and priced
ZERO. Events that render with no asks are cached in the state dir and
re-checked every CV_EMPTY_RECHECK_MIN instead of every pass, which is what
buys back the time to cover the other cities.

IMPORTANT: the prune's live-slug set is built from the FULL in-scope list,
never the budgeted subset — otherwise every event past the budget would be
deleted on each pass and re-added on the next.

PUBLIC LOGS: this repo is public so Actions logs are world-readable. Event
slugs and the scan scope are redacted to stable short hashes unless
CV_LOG_SLUGS=1 (set that only for local runs). Never log a raw slug, a
city list, or a full URL outside that guard.

Env knobs:
    CV_CITIES            regions to scan, comma list; empty = all
    CV_HOME_REGION       priority region (default nyc)
    CV_BUDGET_S          fetch budget per pass (default 600)
    CV_EMPTY_RECHECK_MIN re-check a no-ask event this often (default 120)
    CV_MAX_EVENTS        0 = unlimited (default 0)
    CV_DELAY             per-request delay, jittered (cv_scanner.py)
    CV_STATE_DIR         where the empty-cache lives (default ./.cv_state)
    CV_LOG_FILE          extra log destination (stdout is always used)
    CV_LOG_SLUGS=1       print slugs/scope in the clear (local only)

USAGE:
    python cv_scan.py          # one pass
    python cv_scan.py --dry    # plan + would-be writes and prunes, no writes
-----------------------------------------------------------
"""

import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cv_scanner import (_get, _slug, _is_past, parse_event,
                        SITEMAP, EVENT_RE, REQ_DELAY)
from cv_regions import parse_regions, region_of
import supabase_helper

HERE = Path(__file__).parent
STATE_DIR = Path(os.environ.get("CV_STATE_DIR") or (HERE / ".cv_state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
EMPTY_CACHE_PATH = STATE_DIR / "cv_empty.json"

try:    # Windows consoles default to cp1252 and mangle em dashes to "?"
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# force=True: cv_scanner calls basicConfig at import, which would otherwise
# make this a no-op and leave the log file permanently empty.
_handlers = [logging.StreamHandler(sys.stdout)]
if os.environ.get("CV_LOG_FILE"):
    _handlers.append(logging.FileHandler(os.environ["CV_LOG_FILE"], encoding="utf-8"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=_handlers,
    force=True,
)
log = logging.getLogger(__name__)

# Public repo: keep HTTP client chatter (full request URLs) out of the logs.
for _n in ("httpx", "httpcore", "urllib3", "hpack"):
    logging.getLogger(_n).setLevel(logging.WARNING)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


LOG_SLUGS = os.environ.get("CV_LOG_SLUGS") == "1"
PRUNE_MIN_URLS = 100    # never prune off a suspiciously small sitemap
PRUNE_CEIL_FRAC = 0.35  # nor delete more than this share of stored rows
PRUNE_CEIL_MIN = 75     # ...with a floor, so small tables can still clean up
SCOPE = parse_regions(os.environ.get("CV_CITIES", ""))          # empty = all
# `or`, not a .get default: an unset Actions secret arrives as an EMPTY
# STRING, which would set HOME to the empty set and silently drop the
# home-region priority bucket instead of falling back to nyc.
HOME = parse_regions(os.environ.get("CV_HOME_REGION") or "nyc")
# 600s fits a full sweep of the live sitemap (~250 trading events at ~1.15s)
# inside the workflow's 15-min cycle, which sleeps 900 - elapsed. The PC copy
# runs a much tighter budget because it shares its slot with market_prices.
BUDGET_S = _env_int("CV_BUDGET_S", 600)
EMPTY_RECHECK_MIN = _env_int("CV_EMPTY_RECHECK_MIN", 120)
MAX_EVENTS = _env_int("CV_MAX_EVENTS", 0)                       # 0 = unlimited


def _id(slug: str) -> str:
    """Log-safe event id. Stable across runs, so a hash can still be traced
    back through a local run with CV_LOG_SLUGS=1."""
    if LOG_SLUGS:
        return slug[:56]
    return "#" + hashlib.sha1(slug.encode("utf-8")).hexdigest()[:8]


def _scope_label(regions: set) -> str:
    if LOG_SLUGS:
        return ",".join(sorted(regions)) or "all"
    return "all" if not regions else f"{len(regions)} region(s)"


def _region_label(slug: str) -> str:
    """Only the --dry preview prints per-event regions, and --dry is a local
    tool — but redact anyway so a debugging run on the runner can't publish
    the city breakdown."""
    return region_of(slug) if LOG_SLUGS else "-"


def in_scope(slug: str) -> bool:
    """Empty CV_CITIES means scan every region."""
    return not SCOPE or region_of(slug) in SCOPE


def _all_in_low(html: str) -> float | None:
    """Fee-inclusive lowest ask from the RSC payload (quotes arrive escaped)."""
    for pat in (r'\\?"all_lowest_ask_all_in\\?":\\?"?([0-9]+(?:\.[0-9]+)?)',
                r'\\?"all_in_lowest_ask_price\\?":\\?"?([0-9]+(?:\.[0-9]+)?)'):
        m = re.search(pat, html)
        if m:
            v = float(m.group(1))
            return v if v > 0 else None
    return None


def _rsc_num(html: str, key: str) -> float | None:
    """Numeric field from the escaped RSC payload; None if absent/null."""
    m = re.search(rf'\\?"{key}\\?":([0-9]+(?:\.[0-9]+)?)', html)
    return float(m.group(1)) if m else None


def _volume_signals(html: str) -> dict:
    """Demand/supply/volume fields the event page already carries (verified
    live 2026-07-17): looking_to_go/looking_to_sell counters, event-level
    last_sale ('$$265' — doubled $), max_bid, tickets_remaining, and the ask
    book's depth (each listing renders as "all_in_price":N,"qty":M)."""
    m = re.search(r'\\?"last_sale\\?":\\?"\$+([0-9]+(?:\.[0-9]+)?)', html)
    last_sale = float(m.group(1)) if m else None
    asks = re.findall(r'\\?"all_in_price\\?":([0-9]+(?:\.[0-9]+)?),\\?"qty\\?":([0-9]+)', html)
    ltg = _rsc_num(html, "looking_to_go")
    lts = _rsc_num(html, "looking_to_sell")
    rem = _rsc_num(html, "tickets_remaining")
    return {
        "last_sale": last_sale,
        "looking_to_go": int(ltg) if ltg is not None else None,
        "looking_to_sell": int(lts) if lts is not None else None,
        "tickets_remaining": int(rem) if rem is not None else None,
        "highest_bid": _rsc_num(html, "max_bid"),
        "ask_count": len(asks),
        "ask_qty": sum(int(q) for _, q in asks),
    }


# ---------------------------------------------------------------- scheduling

def _age_min(iso: str, now: datetime) -> float | None:
    """Minutes since an ISO timestamp; None if missing/unparseable."""
    if not iso:
        return None
    try:
        from dateutil import parser as dp
        return (now - dp.parse(iso)).total_seconds() / 60
    except Exception:
        return None


def _load_empty_cache() -> dict:
    """{slug: iso} for events that last rendered with no asks at all.

    On a runner this file survives within one chained job (~5.5h) and is lost
    when the chain restarts, so the first pass of a new chain re-checks every
    event. Entries expire after CV_EMPTY_RECHECK_MIN anyway, so persisting it
    across chains would buy almost nothing.
    """
    try:
        return json.loads(EMPTY_CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_empty_cache(cache: dict, live_slugs: set):
    # drop entries whose event left the sitemap, so the file can't grow forever
    trimmed = {k: v for k, v in cache.items() if k in live_slugs}
    try:
        EMPTY_CACHE_PATH.write_text(json.dumps(trimmed, indent=1))
    except Exception as e:
        log.warning("empty-cache write failed: %s", e)


def _plan(urls: list, prev: dict, empty: dict, now: datetime):
    """Order the pass so a spent budget costs the least. See module docstring
    for why tracked-before-new is deliberate.

    Returns (ordered_urls, skipped_empty, skipped_past). Events whose stored
    event_end is already past are dropped without a fetch — the old loop only
    learned that after paying for the request.
    """
    ordered, skipped_empty, skipped_past = [], 0, 0
    for u in urls:
        slug = _slug(u)
        row = prev.get(slug)

        if row and _is_past(row.get("event_end") or ""):
            skipped_past += 1
            continue

        seen_empty = _age_min(empty.get(slug), now)
        if seen_empty is not None and seen_empty < EMPTY_RECHECK_MIN:
            skipped_empty += 1
            continue

        home = region_of(slug) in HOME
        if row is None:
            bucket, stale = (1 if home else 3), 0.0
        else:
            bucket = 0 if home else 2
            stale = _age_min(row.get("updated_at"), now) or float("inf")
        ordered.append((bucket, -stale, u))

    ordered.sort(key=lambda t: (t[0], t[1]))
    return [u for _, _, u in ordered], skipped_empty, skipped_past


def run(dry: bool = False) -> int:
    now = datetime.now(timezone.utc)
    log.info("-- scan started (scope=%s, home=%s, budget=%ds) --",
             _scope_label(SCOPE), _scope_label(HOME), BUDGET_S)

    sb = supabase_helper.client()
    if sb is None:
        log.error("no Supabase client — SUPABASE_URL/SUPABASE_KEY not set")
        return 1

    try:
        sm = _get(SITEMAP)
    except Exception as e:
        log.error("sitemap fetch failed: %s", e)
        return 1

    all_urls = list(dict.fromkeys(EVENT_RE.findall(sm)))
    in_scope_urls = [u for u in all_urls if in_scope(_slug(u))]
    if MAX_EVENTS:
        in_scope_urls = in_scope_urls[:MAX_EVENTS]
    # Built from the FULL in-scope list — the prune below treats this as
    # "everything that exists", so it must never be the budgeted subset.
    live_slugs = {_slug(u) for u in in_scope_urls}
    log.info("sitemap: %d events, %d in scope", len(all_urls), len(in_scope_urls))

    # Previous pass's rows, for sale detection + change-only history appends.
    try:
        prev = {r["slug"]: r for r in
                (sb.table("cv_prices")
                 .select("slug,low_price,low_price_all_in,last_sale,highest_bid,"
                         "ask_count,ask_qty,updated_at,event_end")
                 .execute().data or [])}
    except Exception as e:
        log.warning("prev read failed (fresh columns?) — no history this pass: %s", e)
        prev = {}

    empty = _load_empty_cache()
    queue, skip_empty, skip_past = _plan(in_scope_urls, prev, empty, now)
    log.info("plan: %d to scan (%d skipped as no-ask, %d skipped as past)",
             len(queue), skip_empty, skip_past)

    if dry:
        for u in queue[:15]:
            s = _slug(u)
            age = _age_min((prev.get(s) or {}).get("updated_at"), now)
            log.info("   [%-13s] %-8s %s", _region_label(s),
                     "NEW" if s not in prev else f"{age:.0f}m", _id(s))
        log.info("   ... %d more", max(0, len(queue) - 15))

    started = time.time()
    scanned = unpriced = sales_seen = hist_rows = fetch_fail = 0
    for i, url in enumerate(queue):
        if time.time() - started > BUDGET_S:
            log.info("budget %ds spent — %d event(s) deferred to next pass "
                     "(they sort stalest-first, so they lead the next one)",
                     BUDGET_S, len(queue) - i)
            break
        slug = _slug(url)
        try:
            html = _get(url)
        except Exception as e:
            fetch_fail += 1
            log.warning("skip %s: %s", _id(slug), e)
            time.sleep(REQ_DELAY)
            continue
        d = parse_event(html)
        if not d:
            # Real event, zero asks/bids/watchers. Remember it so the next
            # few passes can spend their budget on markets that are trading.
            unpriced += 1
            empty[slug] = now.isoformat()
            time.sleep(REQ_DELAY * random.uniform(0.6, 1.4))
            continue
        empty.pop(slug, None)
        if _is_past(d["event_end"]):
            time.sleep(REQ_DELAY * random.uniform(0.6, 1.4))
            continue
        vol = _volume_signals(html)
        row = {
            "slug": slug, "url": url,
            "event_name": d["event_name"], "venue": d["venue"],
            "low_price": d["low_price"], "high_price": d["high_price"],
            "low_price_all_in": _all_in_low(html),
            "availability": d["availability"], "event_end": d["event_end"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **vol,
        }
        p = prev.get(slug)
        if dry:
            scanned += 1
            log.info("   would store [%-13s] $%-7s %s",
                     _region_label(slug), row["low_price"], _id(slug))
            time.sleep(REQ_DELAY * random.uniform(0.6, 1.4))
            continue
        try:
            sb.table("cv_prices").upsert(row, on_conflict="slug").execute()
            scanned += 1
        except Exception as e:
            log.error("save failed for %s: %s", _id(slug), e)
            time.sleep(REQ_DELAY * random.uniform(0.6, 1.4))
            continue

        # A changed last_sale = at least one sale happened since the last pass.
        if (p and vol["last_sale"] is not None and p.get("last_sale") is not None
                and float(p["last_sale"]) != vol["last_sale"]):
            try:
                sb.table("cv_sales_seen").insert(
                    {"slug": slug, "price": vol["last_sale"]}).execute()
                sales_seen += 1
            except Exception as e:
                log.error("cv_sales_seen insert failed for %s: %s", _id(slug), e)

        # Append history only when a tracked signal moved (keeps the table small).
        def _f(v):
            return None if v is None else float(v)
        changed = p is None or any(
            _f(p.get(k)) != _f(row.get(k))
            for k in ("low_price", "low_price_all_in", "last_sale",
                      "highest_bid", "ask_count", "ask_qty"))
        if changed:
            try:
                sb.table("cv_history").insert({
                    "slug": slug,
                    "low_price": row["low_price"],
                    "low_all_in": row["low_price_all_in"],
                    "highest_bid": vol["highest_bid"],
                    "last_sale": vol["last_sale"],
                    "looking_to_go": vol["looking_to_go"],
                    "looking_to_sell": vol["looking_to_sell"],
                    "ask_count": vol["ask_count"],
                    "ask_qty": vol["ask_qty"],
                }).execute()
                hist_rows += 1
            except Exception as e:
                log.error("cv_history insert failed for %s: %s", _id(slug), e)
        time.sleep(REQ_DELAY * random.uniform(0.6, 1.4))

    # Prune rows we are no longer maintaining. Two kinds of zombie:
    #   * slug left the sitemap (event finished, or CrowdVolt re-slugged it)
    #   * slug is outside the current scan scope, so nothing will ever
    #     refresh it — the old code PROTECTED these, which is how 63 rows
    #     ended up frozen at 26 days old and still being read as current.
    # cv_history keeps the price record, so this only drops the live row.
    pruned = 0
    if len(in_scope_urls) >= PRUNE_MIN_URLS:
        try:
            stored = (sb.table("cv_prices").select("slug").execute().data or [])
            dead = [r["slug"] for r in stored
                    if r["slug"] not in live_slugs or not in_scope(r["slug"])]
            # Relative guard on top of PRUNE_MIN_URLS: a partial sitemap
            # response looks exactly like "half the events ended". Deleting is
            # the one irreversible thing this script does, so a suspiciously
            # large batch aborts the prune instead of trusting the fetch.
            # (A legitimate cleanup is small — the first real one was 58/312.)
            # This also catches a misconfigured CV_CITIES: narrowing the scope
            # on one runner would otherwise delete every other region's rows.
            limit = max(PRUNE_CEIL_MIN, int(len(stored) * PRUNE_CEIL_FRAC))
            if len(dead) > limit:
                log.error("prune ABORTED: %d/%d rows looked dead (limit %d) — "
                          "suspect a truncated sitemap or a narrowed CV_CITIES, "
                          "not a real cleanup", len(dead), len(stored), limit)
                dead = []
            if dry:
                log.info("would prune %d dead row(s)", len(dead))
            else:
                for s in dead:
                    sb.table("cv_prices").delete().eq("slug", s).execute()
            pruned = len(dead)
        except Exception as e:
            log.error("prune failed: %s", e)
    else:
        log.warning("only %d in-scope URLs — skipping prune", len(in_scope_urls))

    if not dry:
        _save_empty_cache(empty, live_slugs)

    log.info("-- done in %.0fs%s: %d priced, %d unpriced, %d sale(s) detected, "
             "%d history row(s), %d dead slug(s) pruned --",
             time.time() - started, " [DRY]" if dry else "",
             scanned, unpriced, sales_seen, hist_rows, pruned)

    # Yield check. A pass that fetches a full queue and stores NOTHING is a
    # broken feed, not a quiet market — the site changed shape, the key lost
    # write access, or every request is being blocked. Say so loudly instead
    # of exiting 0, which is how a dead feed once went unnoticed for 21 days.
    if queue and not dry:
        if scanned == 0:
            log.error("ZERO events priced out of %d queued (%d fetch failures) "
                      "— the feed is DOWN, not idle", len(queue), fetch_fail)
            return 1
        if fetch_fail > len(queue) * 0.5:
            log.error("%d/%d fetches failed — suspect a block or an outage",
                      fetch_fail, len(queue))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(run(dry="--dry" in sys.argv))
