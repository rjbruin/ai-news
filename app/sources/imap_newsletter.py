"""IMAP newsletter source: pull newsletter emails from a mailbox.

Config keys (fall back to app-level IMAP_* config when omitted):
  host, port, username, password, folder, mark_seen

Deduplication is handled server-side via IngestRun.external_id (the email's
Message-ID header).  The seen/unseen flag is no longer used for tracking —
mark_seen is kept as an optional inbox-housekeeping courtesy only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import bleach
from bs4 import BeautifulSoup
from flask import current_app

from .base import NewsSource, RawDocument

logger = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_DAYS = 30
# Regular polling now also runs synchronously from a web request (the
# "Check now" button on a pending newsletter subscription), so a hung IMAP
# connection would tie up a request worker rather than just a background job.
_IMAP_TIMEOUT = 30


def html_to_text(html: str) -> str:
    """Strip HTML to readable text, dropping scripts/styles.

    Link targets are preserved: each ``<a href="…">`` becomes ``text (url)`` so
    the downstream LLM extractor can capture the source URL. Plain ``get_text()``
    keeps only the anchor's visible text and discards the href, which is why
    hyperlink-only newsletters previously produced items with no URL.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().startswith(("http://", "https://")):
            continue  # skip mailto:, #anchors, javascript:, etc.
        text = a.get_text(" ", strip=True)
        # Skip when the visible text already contains the URL (nothing to add).
        if href in text:
            continue
        a.replace_with(f"{text} ({href})" if text else href)
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
        "mark_seen": {
            "type": "checkbox",
            "label": "Mark messages as seen after fetching (inbox housekeeping only)",
            "required": False,
            "default": False,
        },
    }

    def _conn_params(self) -> dict:
        cfg = current_app.config
        return {
            "host": self.config.get("host") or cfg.get("IMAP_HOST"),
            "port": int(self.config.get("port") or cfg.get("IMAP_PORT", 993)),
            "username": self.config.get("username") or cfg.get("IMAP_USERNAME"),
            "password": self.config.get("password") or cfg.get("IMAP_PASSWORD"),
            "folder": self.config.get("folder") or cfg.get("IMAP_FOLDER", "INBOX"),
            "mark_seen": bool(self.config.get("mark_seen", False)),
        }

    def fetch(self, since: datetime | None) -> list[RawDocument]:
        from imap_tools import AND, MailBox

        p = self._conn_params()
        if not (p["host"] and p["username"] and p["password"]):
            raise RuntimeError("IMAP source is not configured (host/username/password).")

        # Use a date floor for efficiency — the server-side external_id check in
        # ingest_source() is the real dedup guard, so a small overlap is fine.
        if since is not None:
            floor = since.date()
        else:
            floor = (datetime.utcnow() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).date()
        criteria = AND(date_gte=floor)

        docs: list[RawDocument] = []
        with MailBox(p["host"], port=p["port"], timeout=_IMAP_TIMEOUT).login(
            p["username"], p["password"], initial_folder=p["folder"]
        ) as mailbox:
            for msg in mailbox.fetch(criteria, mark_seen=p["mark_seen"]):
                # Prefer the HTML part: after html_to_text() it carries the link
                # targets, which the plain-text alternative usually lacks. Fall
                # back to the plain-text part only when there is no usable HTML.
                body = html_to_text(msg.html) if msg.html else ""
                if not body.strip():
                    body = (msg.text or "").strip()
                if not body.strip():
                    continue
                # Prefer the stable Message-ID header; fall back to IMAP UID.
                message_id = (msg.headers.get("message-id") or [""])[0].strip()
                external_id = message_id or msg.uid
                docs.append(
                    RawDocument(
                        external_id=external_id,
                        text=body,
                        subject=msg.subject,
                        received_at=msg.date,
                        meta={"from": msg.from_},
                    )
                )
        return docs

    def scan_senders(self) -> list[tuple[str, str]]:
        """Full-mailbox header scan: returns (from_header, subject) for every
        message, ignoring the fetch() lookback window.

        Used by the admin "reindex newsletters" action to discover
        subscriptions whose mail predates per-newsletter splitting, or that
        simply haven't sent anything since the last regular poll. Headers
        only (no body fetch), so this stays cheap even for a large mailbox.
        """
        from imap_tools import MailBox

        p = self._conn_params()
        if not (p["host"] and p["username"] and p["password"]):
            raise RuntimeError("IMAP source is not configured (host/username/password).")

        pairs: list[tuple[str, str]] = []
        with MailBox(p["host"], port=p["port"], timeout=_IMAP_TIMEOUT).login(
            p["username"], p["password"], initial_folder=p["folder"]
        ) as mailbox:
            for msg in mailbox.fetch(headers_only=True, mark_seen=False, bulk=True):
                pairs.append((msg.from_ or "", msg.subject or ""))
        return pairs
