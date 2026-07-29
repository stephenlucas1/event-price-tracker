"""
cv_email.py
-----------------------------------------------------------
Gmail sender for the alert scripts. Env-only, no config file.

The workstation copy of these alerts read an SMTP block out of a local
config.json and pulled the app password from a .env. Neither exists on a
runner, so config comes from three env vars / Actions secrets:

    GMAIL_USER          sender address
    GMAIL_APP_PASSWORD  app password (NOT the account password)
    ALERT_TO            recipient; defaults to GMAIL_USER

Sends multipart text+html so the digest is readable either way. Returns
False rather than raising when unconfigured, so a missing secret degrades
an alert pass to "logged only" instead of failing the whole run.
-----------------------------------------------------------
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger(__name__)

# Values that mean "nobody has filled this in yet". A placeholder must never
# be treated as a credential — it produces a confusing SMTP auth error at
# send time instead of an honest "not configured" at check time.
PLACEHOLDERS = ("PUT-YOUR", "PASTE-", "YOUR", "CHANGEME", "xxxx")


def config() -> dict | None:
    """Resolved mail config, or None if it isn't usable."""
    sender = (os.environ.get("GMAIL_USER") or "").strip()
    pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").strip()
    if not sender or not pw:
        return None
    if any(p.lower() in pw.lower() for p in PLACEHOLDERS):
        log.warning("GMAIL_APP_PASSWORD looks like a placeholder — not sending")
        return None
    return {
        "sender": sender,
        "app_password": pw,
        "recipient": (os.environ.get("ALERT_TO") or "").strip() or sender,
        "smtp_host": os.environ.get("SMTP_HOST") or "smtp.gmail.com",
        "smtp_port": int(os.environ.get("SMTP_PORT") or 465),
    }


def send(subject: str, text: str, html: str = "") -> bool:
    cfg = config()
    if not cfg:
        log.warning("email not configured (GMAIL_USER / GMAIL_APP_PASSWORD)")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["sender"]
    msg["To"] = cfg["recipient"]
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=ctx) as s:
            s.login(cfg["sender"], cfg["app_password"])
            s.send_message(msg)
    except Exception as e:
        # Never log the message body — subjects and bodies carry event names,
        # and these logs are public.
        log.error("email send failed: %s", type(e).__name__)
        return False
    log.info("email sent (%d char body)", len(text))
    return True
