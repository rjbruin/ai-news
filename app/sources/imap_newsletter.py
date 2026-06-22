"""IMAP newsletter source: pull newsletter emails from a mailbox.

Config keys (fall back to app-level IMAP_* config when omitted):
  host, port, username, password, folder, mark_seen
"""
from __future__ import annotations

import logging
from datetime import datetime

import bleach
from bs4 import BeautifulSoup
from flask import current_app

from .base import NewsSource, RawDocument

logger = logging.getLogger(__name__)


def html_to_text(html: str) -> str:
    """Strip HTML to readable text, dropping scripts/styles."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    cleaned = bleach.clean(text, tags=[], strip=True)
    lines = [ln.strip() for ln in cleaned.splitlines()]
    return "\n".join(ln for ln in lines if ln)


class ImapNewsletterSource(NewsSource):
    type_key = "imap_newsletter"
    label = "Newsletter mailbox (IMAP)"
    description = "Fetches newsletter emails from an IMAP mailbox and extracts news."
    config_schema = {
        "host": {"type": "text", "label": "IMAP host", "required": False},
        "port": {"type": "number", "label": "IMAP port", "required": False},
        "username": {"type": "text", "label": "Username", "required": False},
        "password": {"type": "password", "label": "Password", "required": False, "secret": True},
        "folder": {"type": "text", "label": "Folder", "required": False},
        "mark_seen": {"type": "checkbox", "label": "Mark messages as seen after fetching (recommended)", "required": False, "default": True},
    }

    def _conn_params(self) -> dict:
        cfg = current_app.config
        return {
            "host": self.config.get("host") or cfg.get("IMAP_HOST"),
            "port": int(self.config.get("port") or cfg.get("IMAP_PORT", 993)),
            "username": self.config.get("username") or cfg.get("IMAP_USERNAME"),
            "password": self.config.get("password") or cfg.get("IMAP_PASSWORD"),
            "folder": self.config.get("folder") or cfg.get("IMAP_FOLDER", "INBOX"),
            "mark_seen": bool(self.config.get("mark_seen", True)),
        }

    def fetch(self, since: datetime | None) -> list[RawDocument]:
        # Imported lazily so the dependency is only needed when polling.
        from imap_tools import AND, MailBox

        p = self._conn_params()
        if not (p["host"] and p["username"] and p["password"]):
            raise RuntimeError("IMAP source is not configured (host/username/password).")

        docs: list[RawDocument] = []
        # Always filter unseen only. When mark_seen=True (default), processed
        # emails are marked read and won't appear again — no date filter needed.
        # When mark_seen=False, use a date floor to avoid reprocessing old mail.
        if p["mark_seen"] or since is None:
            criteria = AND(seen=False)
        else:
            criteria = AND(date_gte=since.date(), seen=False)

        with MailBox(p["host"], port=p["port"]).login(
            p["username"], p["password"], initial_folder=p["folder"]
        ) as mailbox:
            for msg in mailbox.fetch(criteria, mark_seen=p["mark_seen"]):
                body = msg.text or html_to_text(msg.html or "")
                if not body.strip():
                    continue
                docs.append(
                    RawDocument(
                        external_id=msg.uid or msg.headers.get("message-id", [""])[0],
                        text=body,
                        subject=msg.subject,
                        received_at=msg.date,
                        meta={"from": msg.from_},
                    )
                )
        return docs
