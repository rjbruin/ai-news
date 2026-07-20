"""Document block IR for agentic summaries.

A summary document is a list of blocks (dicts). Each block has a ``type`` and a
stable ``id``, plus type-specific fields. The agent assembles these via editor
tools; the system renders them per channel (see app.agent.render). Keeping the
set of block types fixed means the agent can only produce valid, on-brand
layouts — the "super-WYSIWYG editor using system-defined elements".
"""
from __future__ import annotations

import re
import uuid
from urllib.parse import urlparse

_TAG_RE = re.compile(r"<[^>]+>")

# ── Block schema ──────────────────────────────────────────────────────────
# For each type: required fields, optional fields (with defaults), and any
# enum constraints. Unknown fields are dropped during validation.

STORY_EMPHASIS = ("lead", "standard", "brief")
CALLOUT_VARIANTS = ("trend", "connection", "watch", "note")

BLOCK_SCHEMA: dict[str, dict] = {
    # ── Structural blocks (always available to the agent) ──────────────────
    "edition_header": {
        "required": ["title"],
        "optional": {"subtitle": "", "date": ""},
        "plain_text": ["title", "subtitle"],
    },
    "intro": {
        "required": ["markdown"],
        "optional": {},
    },
    "section": {
        "required": ["title"],
        "optional": {"description": ""},
        "plain_text": ["title", "description"],
    },
    "divider": {
        "required": [],
        "optional": {},
    },
    # ── Content blocks (current generation — agent uses these) ─────────────
    "item": {
        "required": ["headline", "subheader", "summary"],
        "optional": {"item_id": None, "sources": [], "escalated_from_quick_hit": False},
        "plain_text": ["headline", "subheader"],
    },
    "trend": {
        "required": ["headline", "text"],
        "optional": {},
        "plain_text": ["headline"],
    },
    "more_news": {
        "required": ["items"],
        "optional": {},
    },
    # ── Legacy blocks (not exposed to agent; kept for stored-document compat) ──
    "story": {
        "required": ["headline"],
        "optional": {
            "item_id": None,
            "dek": "",
            "body": "",
            "url": "",
            "urls": [],
            "source": "",
            "emphasis": "standard",
        },
        "enums": {"emphasis": STORY_EMPHASIS},
        "plain_text": ["headline", "dek"],
    },
    "cluster": {
        "required": ["headline"],
        "optional": {"item_ids": [], "body": ""},
        "plain_text": ["headline"],
    },
    "callout": {
        "required": ["title", "markdown"],
        "optional": {"variant": "note"},
        "enums": {"variant": CALLOUT_VARIANTS},
        "plain_text": ["title"],
    },
    "quote": {
        "required": ["text"],
        "optional": {"attribution": ""},
        "plain_text": ["text", "attribution"],
    },
    "quick_hits": {
        "required": ["items"],
        "optional": {"title": "Also notable"},
        "plain_text": ["title"],
    },
}

BLOCK_TYPES = tuple(BLOCK_SCHEMA.keys())

# Block types the agent may produce (excludes legacy types kept for compat).
AGENT_BLOCK_TYPES = (
    "edition_header", "intro", "section", "divider",
    "item", "trend", "more_news",
)


class BlockValidationError(ValueError):
    """Raised when a block document fails validation."""


def strip_tags(text: str | None) -> str:
    """Remove any HTML tags from a plain-text field."""
    if not text:
        return text or ""
    return _TAG_RE.sub("", text).strip()


def url_domain(url: str | None) -> str:
    """Bare domain for citing an article (e.g. 'techcrunch.com'), or ''."""
    if not url:
        return ""
    host = urlparse(url).netloc
    if host.startswith("www."):
        host = host[4:]
    return host


def _looks_like_article_url(url: str) -> bool:
    """True for a URL that plausibly points at a specific article rather than
    a bare homepage (e.g. 'https://theverge.com/' or 'https://theverge.com').

    The agent sometimes hand-types a source it doesn't actually have the
    article link for, guessing the site's root domain instead — that's
    misleading (the reader clicks through to the homepage, not the story).
    Reject anything without a real path so those get dropped rather than
    rendered as if they were a real citation.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    return parsed.path not in ("", "/")


def _validate_block(block: dict, index: int) -> dict:
    if not isinstance(block, dict):
        raise BlockValidationError(f"Block {index} is not an object.")
    btype = block.get("type")
    if btype not in BLOCK_SCHEMA:
        raise BlockValidationError(
            f"Block {index} has unknown type {btype!r}. "
            f"Valid types: {', '.join(BLOCK_TYPES)}."
        )
    schema = BLOCK_SCHEMA[btype]

    clean: dict = {"type": btype}
    # Stable id — preserve if present, else mint one.
    clean["id"] = str(block.get("id") or f"b_{uuid.uuid4().hex[:8]}")

    for field in schema["required"]:
        if field not in block or block[field] in (None, ""):
            raise BlockValidationError(
                f"Block {index} ({btype}) is missing required field {field!r}."
            )
        clean[field] = block[field]

    for field, default in schema.get("optional", {}).items():
        clean[field] = block.get(field, default)

    for field, allowed in schema.get("enums", {}).items():
        if clean.get(field) not in allowed:
            raise BlockValidationError(
                f"Block {index} ({btype}) field {field!r}={clean.get(field)!r} "
                f"must be one of {allowed}."
            )

    # Strip HTML tags from plain-text fields. Agents sometimes embed <a> tags
    # in headline/title/dek — those fields are never rendered as HTML so the
    # tags would show as raw text. Markdown fields (body, markdown) are left
    # alone because they intentionally accept inline HTML.
    for field in schema.get("plain_text", []):
        if clean.get(field):
            clean[field] = strip_tags(clean[field])

    # item.sources: agent passes a list of URL strings; normalise to [{url, domain}].
    if btype == "item":
        raw = clean.get("sources")
        if isinstance(raw, str):
            raw = [raw] if raw.strip() else []
        elif not isinstance(raw, list):
            raw = []
        seen_d: set[str] = set()
        normalized: list[dict] = []
        for u in raw:
            if isinstance(u, str) and u.strip():
                u = u.strip()
                d = url_domain(u)
                if d and d not in seen_d and _looks_like_article_url(u):
                    normalized.append({"url": u, "domain": d})
                    seen_d.add(d)
        clean["sources"] = normalized

    # story.sources: derive from url/urls (legacy path — keep for old documents).
    if btype == "story":
        url_list: list[str] = []
        raw_urls = clean.get("urls")
        if isinstance(raw_urls, list):
            url_list = [u for u in raw_urls if isinstance(u, str) and u.strip()]
        primary = clean.get("url", "")
        if primary and primary not in url_list:
            url_list.insert(0, primary)
        seen_domains: set[str] = set()
        sources: list[dict] = []
        for u in url_list:
            d = url_domain(u)
            if d and d not in seen_domains and _looks_like_article_url(u):
                sources.append({"url": u, "domain": d})
                seen_domains.add(d)
        clean["sources"] = sources
        clean["url"] = sources[0]["url"] if sources else ""
        clean["source"] = sources[0]["domain"] if sources else ""

    # more_news.items: list of {headline, url?, item_id?}
    if btype == "more_news":
        norm = []
        for it in clean.get("items") or []:
            if isinstance(it, str):
                norm.append({"headline": it, "url": "", "item_id": None})
            elif isinstance(it, dict) and it.get("headline"):
                url = it.get("url", "")
                if url and not _looks_like_article_url(url):
                    url = ""  # bare homepage guess, not a real article link
                norm.append({
                    "headline": it["headline"],
                    "url": url,
                    "item_id": it.get("item_id"),
                })
        if not norm:
            raise BlockValidationError(
                f"Block {index} (more_news) needs a non-empty items list."
            )
        clean["items"] = norm

    # quick_hits.items normalisation: list of {text, url?} (legacy)
    if btype == "quick_hits":
        norm = []
        for it in clean.get("items") or []:
            if isinstance(it, str):
                norm.append({"text": it, "url": ""})
            elif isinstance(it, dict) and it.get("text"):
                norm.append({"text": it["text"], "url": it.get("url", "")})
        if not norm:
            raise BlockValidationError(
                f"Block {index} (quick_hits) needs a non-empty items list."
            )
        clean["items"] = norm

    return clean


def validate_document(blocks: list) -> list[dict]:
    """Validate + normalise a list of blocks. Returns the cleaned document.

    Raises BlockValidationError on the first invalid block.
    """
    if not isinstance(blocks, list):
        raise BlockValidationError("Document must be a list of blocks.")
    cleaned = [_validate_block(b, i) for i, b in enumerate(blocks)]
    return _dedupe_items(cleaned)


def _dedupe_items(blocks: list[dict]) -> list[dict]:
    """Collapse duplicate item/story blocks that cite the same item_id.

    The agent sometimes features the same news item twice across sections.
    Keep the first occurrence and merge source URLs from later duplicates.
    """
    first_index: dict[int, int] = {}
    out: list[dict] = []
    for block in blocks:
        btype = block.get("type")
        item_id = block.get("item_id") if btype in ("item", "story") else None
        if item_id is not None and item_id in first_index:
            kept = out[first_index[item_id]]
            existing = {s["url"] for s in kept.get("sources") or []}
            for src in block.get("sources") or []:
                if src.get("url") and src["url"] not in existing:
                    kept.setdefault("sources", []).append(src)
                    existing.add(src["url"])
            # Legacy: backfill primary url/source for story blocks.
            if not kept.get("url") and block.get("url"):
                kept["url"] = block["url"]
                kept["source"] = block.get("source", "")
            continue
        if item_id is not None:
            first_index[item_id] = len(out)
        out.append(block)
    return out


def find_block(blocks: list[dict], block_id: str) -> int | None:
    """Return the index of the block with the given id, or None."""
    for i, b in enumerate(blocks):
        if b.get("id") == block_id:
            return i
    return None
