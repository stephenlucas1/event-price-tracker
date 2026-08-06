"""
cv_feed_alarm.py
-----------------------------------------------------------
Tells Abir the CrowdVolt feed has stopped — from the CLOUD, so it works
while the PC is off.

WHY THIS EXISTS: ops_audit is the watchdog, but it runs on the
workstation at 08:30. The whole point of the runner is that the PC may be
off for days, and that is exactly when a cloud failure went unreported —
the 2026-08-04 bot gate froze cv_prices for ~32 h and nothing said so.
A watchdog that only runs on the machine you are trying not to depend on
is not a watchdog.

TWO CALL SITES, because they catch different failures:

  --run-failed   from `if: failure()` in scan.yml. Catches a run that
                 executed and failed (bot gate, bad deploy). Immediate.

  (default)      from watchdog.yml's own cron. Catches SILENCE — the chain
                 dying, a revoked CHAIN_PAT, GitHub throttling everything.
                 Nothing else can catch this: if no run happens, no
                 failure hook fires. This is a dead-man's switch, so it
                 must not share the scan's concurrency group or its
                 schedule.

Exits 1 when it alarms, so the Actions UI is red too rather than relying
on the email alone.
-----------------------------------------------------------
"""

import logging
import os
import sys
from datetime import datetime, timezone

import cv_email
import supabase_helper

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

RUN_URL = (os.environ.get("RUN_URL") or "").strip()


def _max_age_h() -> float:
    try:
        return float(os.environ.get("CV_FEED_MAX_AGE_H") or 6)
    except ValueError:
        return 6.0


def _age_hours():
    sb = supabase_helper.client()
    if sb is None:
        return None, "no Supabase client (SUPABASE_URL / SUPABASE_KEY unset)"
    mins = supabase_helper.feed_age_min(sb)
    if mins is None:
        return None, "could not read cv_prices"
    return mins / 60.0, ""


def _send(subject: str, lines: list) -> bool:
    if RUN_URL:
        lines += ["", f"Run: {RUN_URL}"]
    body = "\n".join(lines) + "\n"
    ok = cv_email.send(subject, body)
    if not ok:
        # Do not swallow this: a watchdog that cannot reach you is no
        # watchdog, and the exit code below is then the only signal left.
        log.error("ALARM COULD NOT BE EMAILED — check GMAIL_* secrets")
    return ok


def run_failed() -> int:
    age_h, why = _age_hours()
    age_txt = f"{age_h:.1f} h old" if age_h is not None else f"unknown ({why})"
    log.error("scan run failed; feed is %s", age_txt)
    _send(
        "CrowdVolt scan FAILED — feed is not being written",
        ["A Price Scan run finished with no successful pass.",
         "",
         f"Newest cv_prices row: {age_txt}",
         "",
         "Most likely CrowdVolt rotated its bot gate again. The fetch",
         "impersonates safari17_0; if that stopped clearing, set the",
         "CV_IMPERSONATE secret to another profile — no code change needed.",
         "",
         "If the runner's datacenter IP is the thing being scored rather",
         "than the fingerprint, set CV_PROXY to a residential proxy",
         "(NOT one of the Dice-account proxies — that risks account",
         "standing, which costs more than a stale feed).",
         "",
         "The PC covers the feed whenever it is on."])
    return 1


def probe_crowdvolt(attempts: int = 3):
    """Can THIS host reach CrowdVolt at all? (ok: bool, detail: str)

    Runs on the watchdog's own schedule and concurrency group, so it answers
    the question the scan job cannot: the scan is a 5.5 h loop holding the
    cv-scan slot, and everything queued behind it is cancelled without ever
    executing — a cancelled run tells you nothing about the gate.

    This is the check that separates "the feed is stale because nothing ran"
    from "the feed is stale because this runner is gated", which need
    completely different fixes. Retries, so one Cloudflare blip cannot page.
    """
    import cv_scanner
    last = ""
    for i in range(attempts):
        try:
            body = cv_scanner._get(cv_scanner.SITEMAP)
            n = body.count("crowdvolt.com/event/")
            return True, (f"reachable — sitemap fetched, {n} event URLs, "
                          f"impersonation {cv_scanner._profile or 'n/a'}")
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:90]}"
            log.warning("probe attempt %d/%d failed: %s", i + 1, attempts, last)
            if i + 1 < attempts:
                import time
                time.sleep(5)
    return False, last


def watchdog() -> int:
    max_h = _max_age_h()

    # Probe BEFORE the staleness verdict: the PC keeps the feed fresh whenever
    # it is on, so a fresh-feed early return would never answer whether THIS
    # host can fetch. A gated runner is a real failure even while the PC is
    # covering — it means the cloud is decorative.
    reachable, detail = probe_crowdvolt()
    log.info("crowdvolt from this runner: %s (%s)",
             "OK" if reachable else "BLOCKED", detail)
    if not reachable:
        _send("CrowdVolt is unreachable FROM THE RUNNER — cloud scan is dead",
              ["This host cannot fetch CrowdVolt even with TLS impersonation.",
               f"Last error: {detail}",
               "",
               "The gate is a Cloudflare managed challenge. The impersonation",
               "is verified from the house, so this means the datacenter IP is",
               "being scored too — the cloud scan cannot work as-is.",
               "",
               "Fix: set the CV_PROXY secret to a residential proxy. NOT one of",
               "the Dice-account IPRoyal proxies — Dice account standing is",
               "worth more than a stale feed.",
               "",
               "Alternatively try another CV_IMPERSONATE profile first; that is",
               "free and sometimes enough."])
        return 1

    age_h, why = _age_hours()
    if age_h is None:
        log.error("watchdog could not determine feed age: %s", why)
        _send("CrowdVolt watchdog: cannot read the feed",
              [f"Could not determine cv_prices age: {why}",
               "",
               "This is itself a failure — the watchdog cannot confirm the",
               "feed is alive, so treat it as down until checked."])
        return 1

    if age_h <= max_h:
        log.info("feed OK: %.1f h old (limit %.1f h)", age_h, max_h)
        return 0

    log.error("feed STALE: %.1f h old (limit %.1f h)", age_h, max_h)
    _send(
        f"CrowdVolt feed is {age_h:.0f} h stale — the scan has stopped",
        [f"Newest cv_prices row is {age_h:.1f} h old (limit {max_h:.0f} h).",
         "",
         f"CrowdVolt IS reachable from this runner ({detail}), so this is not",
         "the bot gate — scan runs are not happening at all. The self-chain is",
         "dead. Usual cause: an expired CHAIN_PAT. The fallback cron is",
         "throttled and will not hold the cadence on its own.",
         "",
         "Fix: reissue the stephenlucas1 PAT and update the CHAIN_PAT secret,",
         "then dispatch Price Scan once to restart the chain."])
    return 1


if __name__ == "__main__":
    sys.exit(run_failed() if "--run-failed" in sys.argv[1:] else watchdog())
