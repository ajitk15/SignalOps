"""Small, provider-neutral SMTP delivery for security emails.

Delivery is optional at runtime: password-reset requests always return the same
public response, while this module sends only when the complete SMTP and public
URL configuration is present. Reset tokens are never logged.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.parse import quote

logger = logging.getLogger("signalops.email")


def _enabled(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    sender: str
    public_url: str
    username: str | None
    password: str | None
    starttls: bool
    use_ssl: bool


def smtp_settings() -> SMTPSettings | None:
    """Return validated mail settings, or None when delivery is not configured."""
    host = (os.getenv("SIGNALOPS_SMTP_HOST") or "").strip()
    sender = (os.getenv("SIGNALOPS_SMTP_FROM") or "").strip()
    public_url = (os.getenv("SIGNALOPS_PUBLIC_URL") or "").strip().rstrip("/")
    username = (os.getenv("SIGNALOPS_SMTP_USERNAME") or "").strip() or None
    password = os.getenv("SIGNALOPS_SMTP_PASSWORD") or None
    if not host or not sender or not public_url:
        return None
    if bool(username) != bool(password):
        logger.warning("SMTP username and password must either both be set or both be empty")
        return None
    try:
        port = int(os.getenv("SIGNALOPS_SMTP_PORT", "587"))
    except ValueError:
        logger.warning("SIGNALOPS_SMTP_PORT is not a number")
        return None
    if port < 1 or port > 65535:
        logger.warning("SIGNALOPS_SMTP_PORT is outside the valid TCP port range")
        return None
    use_ssl = _enabled(os.getenv("SIGNALOPS_SMTP_SSL"))
    starttls = _enabled(os.getenv("SIGNALOPS_SMTP_STARTTLS"), default=not use_ssl)
    return SMTPSettings(
        host=host,
        port=port,
        sender=sender,
        public_url=public_url,
        username=username,
        password=password,
        starttls=starttls,
        use_ssl=use_ssl,
    )


def password_reset_delivery_available() -> bool:
    return smtp_settings() is not None


def password_reset_url(token: str, settings: SMTPSettings | None = None) -> str | None:
    settings = settings or smtp_settings()
    if settings is None:
        return None
    # A fragment is not sent in HTTP requests or Referer headers. JavaScript on
    # the login page reads it and submits the token only to the reset endpoint.
    return f"{settings.public_url}/login#reset={quote(token, safe='')}"


def send_password_reset_email(*, to_email: str, display_name: str,
                              token: str, ttl_minutes: int) -> bool:
    """Send one reset email. False means delivery was unavailable or failed."""
    settings = smtp_settings()
    reset_url = password_reset_url(token, settings)
    if settings is None or reset_url is None:
        logger.warning("password-reset email not sent because SMTP is not configured")
        return False

    message = EmailMessage()
    message["Subject"] = "Reset your SignalAIOps password"
    message["From"] = settings.sender
    message["To"] = to_email
    greeting = display_name.strip() or "there"
    message.set_content(
        f"Hello {greeting},\n\n"
        "A password reset was requested for your SignalAIOps account.\n\n"
        f"Reset your password: {reset_url}\n\n"
        f"This one-time link expires in {ttl_minutes} minutes. "
        "If you did not request it, you can ignore this email.\n"
    )

    try:
        if settings.use_ssl:
            with smtplib.SMTP_SSL(
                    settings.host, settings.port, timeout=10,
                    context=ssl.create_default_context()) as smtp:
                if settings.username:
                    smtp.login(settings.username, settings.password or "")
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings.host, settings.port, timeout=10) as smtp:
                if settings.starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if settings.username:
                    smtp.login(settings.username, settings.password or "")
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException):
        logger.exception("password-reset email delivery failed")
        return False
    return True
