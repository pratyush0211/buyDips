"""Outgoing email via SMTP or an HTTPS API. Falls back to printing the message when nothing is
configured, which is what makes sign-in links work during local development."""

import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

import requests

from . import config

log = logging.getLogger("buyDips.mail")
HTTP_TIMEOUT = 15
API_PROVIDERS = ("brevo", "resend")


def configured() -> bool:
    if config.EMAIL_PROVIDER in API_PROVIDERS:
        return bool(config.EMAIL_API_KEY and config.MAIL_FROM)
    return bool(config.SMTP_USER and config.SMTP_PASSWORD)


def send_email(to: str, subject: str, body: str, unsubscribe_url: Optional[str] = None,
               html: Optional[str] = None) -> bool:
    if not configured():
        log.warning("Email not configured; email to %s not sent.\nSubject: %s\n%s", to, subject, body)
        return False
    try:
        if config.EMAIL_PROVIDER == "brevo":
            _send_brevo(to, subject, body, unsubscribe_url, html)
        elif config.EMAIL_PROVIDER == "resend":
            _send_resend(to, subject, body, unsubscribe_url, html)
        else:
            _send_smtp(to, subject, body, unsubscribe_url, html)
        log.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Email to %s failed (%s): %s", to, config.EMAIL_PROVIDER, exc)
        return False


def _unsub_headers(unsubscribe_url: Optional[str]) -> dict:
    # Required by Gmail/Yahoo for bulk senders: one-click unsubscribe.
    if not unsubscribe_url:
        return {}
    return {"List-Unsubscribe": f"<{unsubscribe_url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


def _send_smtp(to, subject, body, unsubscribe_url, html=None) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"buyDips <{config.MAIL_FROM}>"
    msg["To"] = to
    for k, v in _unsub_headers(unsubscribe_url).items():
        msg[k] = v
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as smtp:
        smtp.login(config.SMTP_USER, config.SMTP_PASSWORD.replace(" ", ""))
        smtp.send_message(msg)


def _send_brevo(to, subject, body, unsubscribe_url, html=None) -> None:
    payload = {
        "sender": {"name": "buyDips", "email": config.MAIL_FROM},
        "to": [{"email": to}],
        "subject": subject,
        "textContent": body,
    }
    if html:
        payload["htmlContent"] = html
    headers = _unsub_headers(unsubscribe_url)
    if headers:
        payload["headers"] = headers
    r = requests.post("https://api.brevo.com/v3/smtp/email", json=payload, timeout=HTTP_TIMEOUT,
                      headers={"api-key": config.EMAIL_API_KEY, "accept": "application/json"})
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")


def _send_resend(to, subject, body, unsubscribe_url, html=None) -> None:
    payload = {"from": f"buyDips <{config.MAIL_FROM}>", "to": [to], "subject": subject, "text": body}
    if html:
        payload["html"] = html
    headers = _unsub_headers(unsubscribe_url)
    if headers:
        payload["headers"] = headers
    r = requests.post("https://api.resend.com/emails", json=payload, timeout=HTTP_TIMEOUT,
                      headers={"Authorization": f"Bearer {config.EMAIL_API_KEY}"})
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
