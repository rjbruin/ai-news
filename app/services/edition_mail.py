"""Sends edition-recipient confirmation/notification mail from the shared
newsletter mailbox address, rather than the app's regular outgoing SMTP_*
sender — so the recipient sees mail coming from the same address the actual
edition/newsletter content comes from.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from flask import current_app

logger = logging.getLogger(__name__)


def send_via_newsletter_mailbox(to: str, subject: str, body: str) -> bool:
    cfg = current_app.config
    host = cfg.get("IMAP_SMTP_HOST")
    username = cfg.get("IMAP_USERNAME")
    password = cfg.get("IMAP_PASSWORD")

    if not (host and username and password):
        logger.warning(
            "Newsletter mailbox SMTP not configured — would send to %s:\nSubject: %s\n\n%s",
            to, subject, body,
        )
        return False

    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, cfg.get("IMAP_SMTP_PORT", 587), timeout=30) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("Failed to send newsletter-mailbox email to %s: %s", to, exc)
        return False
