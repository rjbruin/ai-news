"""SMTP email sending (magic links, verification).

In development / when SMTP is unconfigured, emails are logged instead of sent so
the flow still works locally.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from flask import current_app

logger = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str) -> bool:
    cfg = current_app.config
    host = cfg.get("SMTP_HOST")
    username = cfg.get("SMTP_USERNAME")
    password = cfg.get("SMTP_PASSWORD")

    if not (host and username and password):
        logger.warning(
            "SMTP not configured — would send to %s:\nSubject: %s\n\n%s",
            to, subject, body,
        )
        return False

    msg = EmailMessage()
    msg["From"] = cfg.get("MAIL_FROM", username)
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, cfg.get("SMTP_PORT", 587), timeout=30) as smtp:
            if cfg.get("SMTP_USE_TLS", True):
                smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("Failed to send email to %s: %s", to, exc)
        return False
