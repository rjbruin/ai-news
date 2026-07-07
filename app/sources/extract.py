"""Natural-language extraction: turn a raw document (e.g. a newsletter email)
into a list of discrete news items using the LLM with structured output.
"""
from __future__ import annotations

import logging

from ..llm import openrouter
from ..llm.prompt_safety import ANTI_INJECTION_NOTE, wrap_untrusted
from .base import ExtractedItem, RawDocument

logger = logging.getLogger(__name__)

_ITEM_TYPES = "paper, announcement, blog, news, tool, opinion, other"

_SYSTEM = (
    "You extract distinct AI/tech news items from a newsletter email. "
    "For each story return:\n"
    "  - title: the headline (concise, factual)\n"
    "  - one_liner: a single sentence (max 20 words) capturing the single most important "
    "takeaway — must add information NOT already obvious from the title\n"
    "  - summary: 2-4 sentences drawn ONLY from the newsletter text. Do NOT restate the "
    "title or one_liner. Provide context, background, implications, or details that "
    "complement the headline — think of it as the paragraph a reader skims after the title "
    "to decide whether to click through\n"
    f"  - item_type: one of [{_ITEM_TYPES}]\n"
    "  - url: link to the original source article (empty string if none)\n"
    "  - text: full verbatim text of the item ONLY when url is empty (opinion pieces, "
    "editorials, etc.); otherwise empty string\n\n"
    "Ignore ads, sponsorships, job listings, and unsubscribe/footer boilerplate.\n\n"
    'Respond ONLY with valid JSON in this exact format: '
    '{"items": [{"title": "...", "one_liner": "...", "summary": "...", '
    '"item_type": "news", "url": "...", "text": ""}, ...]}\n\n'
    + ANTI_INJECTION_NOTE
)


def extract_items(
    doc: RawDocument,
    *,
    api_key: str | None = None,
    model: str | None = None,
    usage_hook=None,
) -> list[ExtractedItem]:
    """Extract items via LLM. Returns [] if no LLM credentials are available.

    ``api_key``/``model`` let a source's assigned ApiKey drive extraction
    instead of the global OPENROUTER_API_KEY; ``usage_hook`` reports token/cost
    usage back to the caller for per-source/per-key accounting.
    """
    if not api_key and not openrouter.is_configured():
        logger.warning("LLM not configured; skipping extraction for %s", doc.external_id)
        return []

    user_content = wrap_untrusted(
        f"Newsletter subject: {doc.subject or '(none)'}\n\n"
        f"Newsletter body:\n{doc.text[:20000]}"
    )
    try:
        result = openrouter.chat_json(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_content},
            ],
            api_key=api_key,
            model=model,
            usage_hook=usage_hook,
        )
    except openrouter.LLMError as exc:
        logger.error("Extraction failed for %s: %s", doc.external_id, exc)
        return []

    valid_types = set(_ITEM_TYPES.replace(" ", "").split(","))
    items: list[ExtractedItem] = []
    for raw in (result or {}).get("items", []):
        title = (raw.get("title") or "").strip()
        if not title:
            continue
        raw_type = (raw.get("item_type") or "other").strip().lower()
        url = (raw.get("url") or "").strip() or None
        items.append(
            ExtractedItem(
                title=title,
                summary=(raw.get("summary") or "").strip(),
                one_liner=(raw.get("one_liner") or "").strip() or None,
                item_type=raw_type if raw_type in valid_types else "other",
                url=url,
                full_text=(raw.get("text") or "").strip() if not url else None,
                published_at=doc.received_at,
            )
        )
    return items
