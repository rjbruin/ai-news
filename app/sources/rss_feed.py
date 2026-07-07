"""RSS/Atom feed source: pull items from a single feed URL.

Feed entries are already discrete news items (title, link, summary, date),
so this source overrides ``extract()`` to build items directly from feed
metadata — unlike imap_newsletter, no LLM call is needed to split a single
document into multiple stories.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import feedparser
import httpx
from bs4 import BeautifulSoup

from .base import ExtractedItem, NewsSource, RawDocument

logger = logging.getLogger(__name__)

_VALID_ITEM_TYPES = {"paper", "announcement", "blog", "news", "tool", "opinion", "other"}
# Bounds how many entries a single poll ingests — protects against a feed
# that serves its full historical archive on first add. Ongoing polls only
# see genuinely new entries anyway, via the external_id dedup in ingest_source().
_MAX_ENTRIES = 50
_USER_AGENT = "Dispatch/1.0 (+https://github.com/rjbruin/ai-news)"


def _strip_html(html: str) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text(separator=" ")
    # get_text(separator=" ") inserts a space between adjacent text nodes
    # (e.g. after an inline <b>...</b>), which leaves a stray space before
    # trailing punctuation like "world ." — collapse that back together.
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    return " ".join(text.split())


def _entry_datetime(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


class RssFeedSource(NewsSource):
    type_key = "rss_feed"
    label = "RSS / Atom feed"
    description = "Fetches items from a single RSS or Atom feed URL."
    config_schema = {
        "url": {"type": "text", "label": "Feed URL", "required": True},
        "item_type": {
            "type": "text",
            "label": "Item type override (paper, announcement, blog, news, tool, opinion, other — default news)",
            "required": False,
        },
    }

    def fetch(self, since: datetime | None) -> list[RawDocument]:
        url = (self.config.get("url") or "").strip()
        if not url:
            raise RuntimeError("RSS source is not configured (missing feed URL).")

        resp = httpx.get(url, timeout=20.0, follow_redirects=True, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            raise RuntimeError(f"Could not parse feed: {parsed.get('bozo_exception', 'invalid XML')}")

        docs = []
        for entry in parsed.entries[:_MAX_ENTRIES]:
            external_id = (entry.get("id") or entry.get("link") or entry.get("title") or "").strip()
            if not external_id:
                continue
            docs.append(
                RawDocument(
                    external_id=external_id,
                    text=entry.get("summary") or entry.get("description") or "",
                    subject=(entry.get("title") or "").strip() or None,
                    received_at=_entry_datetime(entry),
                    meta={"link": entry.get("link") or None},
                )
            )
        return docs

    def extract(self, doc: RawDocument) -> list[ExtractedItem]:
        item_type = (self.config.get("item_type") or "news").strip().lower()
        if item_type not in _VALID_ITEM_TYPES:
            item_type = "news"
        url = (doc.meta or {}).get("link") or None
        summary = _strip_html(doc.text)[:2000]
        return [
            ExtractedItem(
                title=doc.subject or url or doc.external_id,
                summary=summary,
                url=url,
                published_at=doc.received_at,
                item_type=item_type,
                full_text=summary if not url else None,
            )
        ]
