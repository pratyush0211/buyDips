"""Outgoing email. Falls back to printing the message when SMTP isn't configured, which is
what makes sign-in links work during local development."""

import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

from . import config

log = logging.getLogger("buyDips.mail")


def smtp_configured() -> bool:
    return bool(config.SMTP_USER and config.SMTP_PASSWORD)


def send_email(to: str, subject: str, body: str, unsubscribe_url: Optional[str] = None) -> bool:
    if not smtp_configured():
        log.warning("SMTP not configured; email to %s not sent.\nSubject: %s\n%s", to, subject, body)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"buyDips <{config.MAIL_FROM}>"
    msg["To"] = to
    if unsubscribe_url:
        # Required by Gmail/Yahoo for bulk senders: one-click unsubscribe.
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD.replace(" ", ""))
            smtp.send_message(msg)
        log.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Email to %s failed: %s", to, exc)
        return False
