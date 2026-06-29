"""Document block IR for agentic summaries.

A summary document is a list of blocks (dicts). Each block has a ``type`` and a
stable ``id``, plus type-specific fields. The agent assembles these via editor
tools; the system renders them per channel (see app.agent.render). Keeping the
set of block types fixed means the agent can only produce valid, on-brand
layouts — the "super-WYSIWYG editor using system-defined elements".
"""
from __future__ import annotations

import uuid

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
    return [_validate_block(b, i) for i, b in enumerate(blocks)]


def find_block(blocks: list[dict], block_id: str) -> int | None:
    """Return the index of the block with the given id, or None."""
    for i, b in enumerate(blocks):
        if b.get("id") == block_id:
            return i
    return None
