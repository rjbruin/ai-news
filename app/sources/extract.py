"""Natural-language extraction: turn a raw document (e.g. a newsletter email)
into a list of discrete news items using the LLM with structured output.

Per project decision Q7, we keep only the newsletter-provided summary text and
the URL to the source article — no full-article scraping.
"""
from __future__ import annotations

import logging

from ..llm import openrouter
from .base import ExtractedItem, RawDocument

logger = logging.getLogger(__name__)

_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["title", "summary", "url"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You extract distinct AI/tech news items from a newsletter email. "
    "For each story, return its headline as `title`, a 1-3 sentence neutral "
    "`summary` drawn ONLY from the newsletter text (do not invent facts), and "
    "the `url` linking to the original source article (empty string if none). "
    "Ignore ads, sponsorships, job listings, and unsubscribe/footer boilerplate."
)


def extract_items(doc: RawDocument) -> list[ExtractedItem]:
    """Extract items via LLM. Returns [] if the LLM is not configured."""
    if not openrouter.is_configured():
        logger.warning("LLM not configured; skipping extraction for %s", doc.external_id)
        return []

    user_content = (
        f"Newsletter subject: {doc.subject or '(none)'}\n\n"
        f"Newsletter body:\n{doc.text[:20000]}"
    )
    try:
        result = openrouter.chat_json(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_content},
            ],
            schema=_EXTRACTION_SCHEMA,
        )
    except openrouter.LLMError as exc:
        logger.error("Extraction failed for %s: %s", doc.external_id, exc)
        return []

    items: list[ExtractedItem] = []
    for raw in (result or {}).get("items", []):
        title = (raw.get("title") or "").strip()
        if not title:
            continue
        items.append(
            ExtractedItem(
                title=title,
                summary=(raw.get("summary") or "").strip(),
                url=(raw.get("url") or "").strip() or None,
                published_at=doc.received_at,
            )
        )
    return items
