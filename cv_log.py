"""
cv_log.py
-----------------------------------------------------------
Logging setup + identifier redaction, shared by every script here.

This repo is public, so Actions logs are world-readable. Event slugs, the
region breakdown and the scan scope all describe which markets are being
watched, so they are replaced with stable short hashes unless
CV_LOG_SLUGS=1 (set that only for local runs, never on a runner).

The hashes are stable across runs and hosts, so a hash in a public log can
still be matched to a real event by re-running locally with slugs on.

Alert EMAIL bodies are not redacted — they go to one mailbox, and a digest
of anonymous hashes would be useless.
-----------------------------------------------------------
"""

import hashlib
import logging
import os
import sys

from cv_regions import region_of

LOG_SLUGS = os.environ.get("CV_LOG_SLUGS") == "1"


def setup(name: str) -> logging.Logger:
    """Configure root logging once and return a named logger.

    force=True because some modules call basicConfig at import time, which
    would otherwise make a later call a silent no-op and leave the log file
    permanently empty.
    """
    try:    # Windows consoles default to cp1252 and mangle em dashes to "?"
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    handlers = [logging.StreamHandler(sys.stdout)]
    if os.environ.get("CV_LOG_FILE"):
        handlers.append(logging.FileHandler(os.environ["CV_LOG_FILE"],
                                            encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
    # Keep HTTP client chatter (full request URLs) out of public logs.
    for n in ("httpx", "httpcore", "urllib3", "hpack"):
        logging.getLogger(n).setLevel(logging.WARNING)
    return logging.getLogger(name)


def event_id(slug: str) -> str:
    """Log-safe event identifier."""
    if LOG_SLUGS:
        return slug[:56]
    return "#" + hashlib.sha1((slug or "").encode("utf-8")).hexdigest()[:8]


def region_label(slug: str) -> str:
    return region_of(slug) if LOG_SLUGS else "-"


def scope_label(regions) -> str:
    if LOG_SLUGS:
        return ",".join(sorted(regions)) or "all"
    return "all" if not regions else f"{len(regions)} region(s)"
