"""Minimal Supabase client wrapper. No-op if env vars not set."""

import os
import logging

log = logging.getLogger(__name__)


def client():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception as e:
        log.error("Supabase init failed: %s", e)
        return None
