"""Document block IR for agentic summaries.

A summary document is a list of blocks (dicts). Each block has a ``type`` and a
stable ``id``, plus type-specific fields. The agent assembles these via editor
tools; the system renders them per channel (see app.agent.render). Keeping the
set of block types fixed means the agent can only produce valid, on-brand
layouts — the "super-WYSIWYG editor using system-defined elements".
"""
from __future__ import annotations

import uuid
from urllib.parse import urlparse

# ── Block schema ──────────────────────────────────────────────────────────
# For each type: required fields, optional fields (with defaults), and any
# enum constraints. Unknown fields are dropped during validation.

STORY_EMPHASIS = ("lead", "standard", "brief")
CALLOUT_VARIANTS = ("trend", "connection", "watch", "note")

BLOCK_SCHEMA: dict[str, dict] = {
    "edition_header": {
        "required": ["title"],
        "optional": {"subtitle": "", "date": ""},
    },
    "intro": {
        "required": ["markdown"],
        "optional": {},
    },
    "section": {
        "required": ["title"],
        "optional": {"description": ""},
    },
    "story": {
        "required": ["headline"],
        "optional": {
            "item_id": None,
            "dek": "",
            "body": "",
            "url": "",
            "source": "",
            "emphasis": "standard",
        },
        "enums": {"emphasis": STORY_EMPHASIS},
    },
    "cluster": {
        "required": ["headline"],
        "optional": {"item_ids": [], "body": ""},
    },
    "callout": {
        "required": ["title", "markdown"],
        "optional": {"variant": "note"},
        "enums": {"variant": CALLOUT_VARIANTS},
    },
    "quote": {
        "required": ["text"],
        "optional": {"attribution": ""},
    },
    "quick_hits": {
        "required": ["items"],
        "optional": {"title": "Also notable"},
    },
    "divider": {
        "required": [],
        "optional": {},
    },
}

BLOCK_TYPES = tuple(BLOCK_SCHEMA.keys())


class BlockValidationError(ValueError):
    """Raised when a block document fails validation."""


def url_domain(url: str | None) -> str:
    """Bare domain for citing an article (e.g. 'techcrunch.com'), or ''."""
    if not url:
        return ""
    host = urlparse(url).netloc
    if host.startswith("www."):
        host = host[4:]
    return host


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

    # story.source is never trusted from the agent — it's derived from url so
    # it can't drift to a hallucinated or stale label (e.g. echoing item_type,
    # or the ingestion feed name). No url means no source, deliberately.
    if btype == "story":
        clean["source"] = url_domain(clean.get("url"))

    # quick_hits.items normalisation: list of {text, url?}
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
    return _dedupe_stories(cleaned)


def _dedupe_stories(blocks: list[dict]) -> list[dict]:
    """Collapse duplicate `story` blocks that cite the same item_id.

    When drafting a long, multi-section document the agent sometimes features
    the same item twice under different sections, independently, so citation
    completeness can differ between the copies. Keep the first occurrence,
    fill in its url/source from a later duplicate if that one has it, and
    drop the rest rather than showing the same story twice.
    """
    first_index: dict[int, int] = {}
    out: list[dict] = []
    for block in blocks:
        item_id = block.get("item_id") if block.get("type") == "story" else None
        if item_id is not None and item_id in first_index:
            kept = out[first_index[item_id]]
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
