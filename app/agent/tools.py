"""Agent tools: OpenAI-style tool specs + a dispatcher.

Tools fall into three groups:
  data    — read news items, past editions, recent headlines
  editor  — build/edit the block document (the "super-WYSIWYG" surface)
  memory  — read/consolidate the Markdown memory files

Handlers receive (session, **args) and return a JSON-serialisable result.
Every result is wrapped by ``dispatch`` so errors come back to the model as
correctable tool output rather than crashing the run.
"""
from __future__ import annotations

import json

from ..models import SummaryRun
from . import memory
from .blocks import BlockValidationError, find_block, url_domain, validate_document
from .context import AgentSession

# ── Item serialisation ──────────────────────────────────────────────────────

def _item_brief(item, topics: list[str] | None = None, prior: list | None = None) -> dict:
    # Note: deliberately NOT exposing item.source.name (the ingestion feed's
    # display name, e.g. "Newsletters from you@gmail.com") — it's config
    # metadata about how the item was fetched, not a per-article attribution,
    # and the agent was citing it verbatim when no other name was available.
    # url_domain gives it the actual thing the prompt asks it to cite.
    d = {
        "id": item.id,
        "title": item.title,
        "one_liner": item.one_liner,
        "item_type": item.item_type,
        "topics": topics or [],
        "url": item.url,
        "url_domain": url_domain(item.url) or None,
        "published_at": (item.published_at or item.fetched_at).isoformat()
        if (item.published_at or item.fetched_at) else None,
    }
    # Only present when an earlier edition already covered this story — see
    # services/story_dedup.py. Omitted entirely otherwise so the overwhelming
    # majority of items carry no extra tokens.
    if prior:
        d["prior_coverage"] = [m.as_dict() for m in prior]
    return d


def _item_full(item, topics: list[str] | None = None, prior: list | None = None) -> dict:
    # full_text is deliberately not exposed here — it's NULL for the
    # overwhelming majority of items (this app doesn't scrape full article
    # bodies), so it was pure dead weight in every get_item response.
    d = _item_brief(item, topics, prior)
    d["summary_text"] = item.summary_text
    return d


# ── Data tools ──────────────────────────────────────────────────────────────

def t_list_scope_items(session: AgentSession) -> dict:
    return {
        "count": len(session.items),
        "items": [
            _item_brief(
                i, session.item_tags.get(i.id, []), session.prior_coverage.get(i.id)
            )
            for i in session.items
        ],
    }


def t_get_item(session: AgentSession, item_id: int) -> dict:
    item = session.item_by_id(item_id)
    if item is None:
        return {"error": f"No in-scope item with id {item_id}."}
    return _item_full(
        item, session.item_tags.get(item_id, []), session.prior_coverage.get(item_id)
    )


def t_list_past_editions(session: AgentSession, limit: int = 10, offset: int = 0) -> dict:
    q = (
        # kind: the daily editor's continuity checks are against past editions;
        # a review is a different artefact and would just confuse the picture.
        SummaryRun.query.filter_by(summary_id=session.summary.id, kind="edition")
        .filter(SummaryRun.document.isnot(None))
        .order_by(SummaryRun.generated_at.desc())
        .offset(max(0, offset))
        .limit(min(50, max(1, limit)))
    )
    runs = q.all()
    return {
        "editions": [
            {
                "run_id": r.id,
                "label": r.label,
                "generated_at": r.generated_at.isoformat() if r.generated_at else None,
                "item_count": r.item_count,
                "revision": r.revision,
            }
            for r in runs
        ]
    }


def t_get_edition(session: AgentSession, run_id: int) -> dict:
    run = SummaryRun.query.filter_by(
        id=run_id, summary_id=session.summary.id
    ).first()
    if run is None:
        return {"error": f"No edition with run_id {run_id} for this summary."}
    return {
        "run_id": run.id,
        "label": run.label,
        "generated_at": run.generated_at.isoformat() if run.generated_at else None,
        "document": run.document or [],
    }


def t_read_headlines(session: AgentSession, days: int = 7) -> dict:
    rows = memory.recent_headlines(session.user, session.summary, days=days)
    return {
        "headlines": [
            {"edition_ts": r.edition_ts.isoformat() if r.edition_ts else None,
             "content": r.content}
            for r in rows
        ]
    }


def t_list_editions_in_scope(session: AgentSession) -> dict:
    """The review editor's view of the period: one skeleton per edition.

    Deliberately not the full documents — see services/review_digest.py.
    """
    from ..services.review_digest import digest_for_range

    digests = digest_for_range(
        session.summary, session.range_start, session.range_end,
    )
    return {"count": len(digests), "editions": digests}


def t_get_edition_item(session: AgentSession, run_id: int, block_id: str) -> dict:
    """Full text of ONE featured story from a past edition.

    This is the review editor's only route to item bodies; there is no
    equivalent of get_edition in review mode, because returning whole documents
    would defeat the digest's entire purpose.
    """
    from ..services.review_digest import find_item_block, item_block_payload

    run = SummaryRun.query.filter_by(
        id=run_id, summary_id=session.summary.id, kind="edition",
    ).first()
    if run is None:
        return {"error": f"No edition with run_id {run_id} for this Dispatch."}
    block = find_item_block(run, block_id)
    if block is None:
        return {"error": f"No featured item {block_id!r} in edition {run_id}."}
    payload = item_block_payload(block)
    payload["run_id"] = run.id
    payload["edition_date"] = (
        run.generated_at.date().isoformat() if run.generated_at else None
    )
    return payload


def t_read_coverage(session: AgentSession, days: int = 14) -> dict:
    """The item-linked record of what past editions actually cited.

    Complements read_headlines: that returns the agent's own prose about an
    edition, this returns exactly which articles ran, so 'have we covered
    this?' can be answered against titles rather than paraphrase.
    """
    records = memory.recent_coverage(session.user, session.summary, days=days)
    return {
        "count": len(records),
        "covered": [
            {
                "item_id": r["item_id"],
                "title": r.get("title") or "",
                "covered_on": r["edition_ts"].date().isoformat()
                if hasattr(r.get("edition_ts"), "date") else None,
            }
            for r in records
        ],
    }


# ── Editor tools ────────────────────────────────────────────────────────────

def _require_dict(value, what: str) -> None:
    """Raise a clean, model-readable error instead of letting a wrong-typed
    argument (e.g. a JSON-encoded string instead of an object) crash deeper
    in with a raw Python exception name/message — that kind of confusing
    error has been observed sending the model into an expensive
    trial-and-error loop trying to rediscover the schema by guessing."""
    if not isinstance(value, dict):
        raise BlockValidationError(
            f"{what} must be a JSON object with the documented fields, "
            f"not a {type(value).__name__}."
        )


def _apply_item_sources(session: AgentSession, block: dict) -> dict:
    """If a block cites an in-scope item_id, force its sources to that
    item's real URL — regardless of what the model typed. The model
    already sees each item's real url via list_scope_items/get_item; this
    removes any need (and cost/risk of a typo or hallucinated link) for it
    to retype the URL by hand. Blocks with no item_id (e.g. a story
    spanning multiple sources) keep whatever sources the model supplied.

    Also covers more_news: any entry tagged with item_id gets its url
    forced the same way, so a quick hit citing a real in-scope item never
    ends up linking to a guessed homepage instead of the article."""
    _require_dict(block, "block")
    if block.get("type") == "item":
        item_id = block.get("item_id")
        if item_id is None:
            return block
        item = session.item_by_id(item_id)
        if item is not None and item.url:
            block = {**block, "sources": [item.url]}
        return block
    if block.get("type") == "more_news":
        items = block.get("items")
        if not isinstance(items, list):
            return block
        new_items = []
        changed = False
        for it in items:
            if isinstance(it, dict) and it.get("item_id") is not None:
                item = session.item_by_id(it["item_id"])
                if item is not None and item.url and it.get("url") != item.url:
                    it = {**it, "url": item.url}
                    changed = True
            new_items.append(it)
        if changed:
            block = {**block, "items": new_items}
        return block
    return block


def t_get_document(session: AgentSession, full: bool = False) -> dict:
    if full:
        return {"blocks": session.document}
    # Compact by default — just enough to reference existing blocks for
    # update_block/remove_block/move_block. The model already knows what it
    # wrote (it's still in the conversation); re-injecting the full document
    # on every check was one of the biggest sources of redundant tokens.
    return {"blocks": [{"id": b.get("id"), "type": b.get("type")} for b in session.document]}


def t_set_document(session: AgentSession, blocks: list) -> dict:
    blocks = [_apply_item_sources(session, b) for b in blocks]
    session.document = validate_document(blocks)
    return {"ok": True, "block_count": len(session.document)}


def t_add_block(session: AgentSession, block: dict, index: int | None = None) -> dict:
    block = _apply_item_sources(session, block)
    validated = validate_document([block])[0]
    if index is None or index >= len(session.document):
        session.document.append(validated)
    else:
        session.document.insert(max(0, index), validated)
    return {"ok": True, "block_id": validated["id"], "block_count": len(session.document)}


def t_update_block(session: AgentSession, block_id: str, fields: dict) -> dict:
    _require_dict(fields, "fields")
    idx = find_block(session.document, block_id)
    if idx is None:
        return {"error": f"No block with id {block_id}."}
    merged = {**session.document[idx], **fields, "type": session.document[idx]["type"], "id": block_id}
    merged = _apply_item_sources(session, merged)
    session.document[idx] = validate_document([merged])[0]
    return {"ok": True, "block_id": block_id}


def t_remove_block(session: AgentSession, block_id: str) -> dict:
    idx = find_block(session.document, block_id)
    if idx is None:
        return {"error": f"No block with id {block_id}."}
    session.document.pop(idx)
    return {"ok": True, "block_count": len(session.document)}


def t_move_block(session: AgentSession, block_id: str, to_index: int) -> dict:
    idx = find_block(session.document, block_id)
    if idx is None:
        return {"error": f"No block with id {block_id}."}
    block = session.document.pop(idx)
    session.document.insert(max(0, min(to_index, len(session.document))), block)
    return {"ok": True, "block_count": len(session.document)}


# ── Memory tools ────────────────────────────────────────────────────────────

_WRITABLE_KINDS = ("interests", "content_config", "history")


def t_read_memory(session: AgentSession, kind: str) -> dict:
    if kind not in _WRITABLE_KINDS:
        return {"error": f"kind must be one of {_WRITABLE_KINDS}."}
    return {"kind": kind, "content": memory.read(session.user, session.summary, kind)}


def t_write_memory(session: AgentSession, kind: str, content: str) -> dict:
    if kind not in _WRITABLE_KINDS:
        return {"error": f"kind must be one of {_WRITABLE_KINDS}."}
    memory.write(session.user, session.summary, kind, content)
    return {"ok": True, "kind": kind}


def t_append_history(session: AgentSession, note: str) -> dict:
    existing = memory.read(session.user, session.summary, "history")
    updated = (existing.rstrip() + "\n" + note.strip() + "\n") if existing else note.strip() + "\n"
    memory.write(session.user, session.summary, "history", updated)
    return {"ok": True}


def t_write_headlines(session: AgentSession, notes: str) -> dict:
    # Persisted by the caller after the edition is saved (needs its timestamp).
    session.pending_headlines = notes
    return {"ok": True}


# ── Registry ────────────────────────────────────────────────────────────────

_HANDLERS = {
    "list_scope_items": t_list_scope_items,
    "get_item": t_get_item,
    "list_past_editions": t_list_past_editions,
    "get_edition": t_get_edition,
    "read_headlines": t_read_headlines,
    "read_coverage": t_read_coverage,
    "list_editions_in_scope": t_list_editions_in_scope,
    "get_edition_item": t_get_edition_item,
    "get_document": t_get_document,
    "set_document": t_set_document,
    "add_block": t_add_block,
    "update_block": t_update_block,
    "remove_block": t_remove_block,
    "move_block": t_move_block,
    "read_memory": t_read_memory,
    "write_memory": t_write_memory,
    "append_history": t_append_history,
    "write_headlines": t_write_headlines,
}


def dispatch(name: str, args: dict, session: AgentSession) -> str:
    """Execute a tool by name; always returns a JSON string for the model."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool {name!r}."})
    try:
        result = handler(session, **(args or {}))
    except BlockValidationError as exc:
        result = {"error": f"Invalid block: {exc}"}
    except TypeError as exc:
        result = {"error": f"Bad arguments for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001
        result = {"error": f"{type(exc).__name__}: {exc}"}
    return json.dumps(result, default=str)


# ── Tool specs (OpenAI function-calling schema) ─────────────────────────────

_BLOCK_DESC = (
    "A document block. type is one of: edition_header, intro, section, item, "
    "trend, more_news, divider. See the system prompt for each type's fields."
)

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "list_scope_items",
        "description": "List all news items in scope for this edition (compact form).",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_item",
        "description": "Get the full text and details of one in-scope news item.",
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "integer"}}, "required": ["item_id"]},
    }},
    {"type": "function", "function": {
        "name": "list_past_editions",
        "description": "List previous editions of this summary (for trends/continuity).",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer"}, "offset": {"type": "integer"}}},
    }},
    {"type": "function", "function": {
        "name": "get_edition",
        "description": "Get the full block document of a past edition by run_id.",
        "parameters": {"type": "object", "properties": {
            "run_id": {"type": "integer"}}, "required": ["run_id"]},
    }},
    {"type": "function", "function": {
        "name": "read_headlines",
        "description": "Read brief headline notes from recent editions, to avoid re-reporting the same news.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer"}}},
    }},
    {"type": "function", "function": {
        "name": "read_coverage",
        "description": (
            "List the exact articles past editions cited, with the date each ran. "
            "Use to check whether a story has already been covered — this is the "
            "item-linked record, more reliable than the prose headline notes."
        ),
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer"}}},
    }},
    {"type": "function", "function": {
        "name": "get_document",
        "description": (
            "Return the current draft document. By default returns a compact "
            "list of {id, type} only — you already know what you wrote, this "
            "is just for referencing ids. Pass full=true only if you actually "
            "need to re-read block content."
        ),
        "parameters": {"type": "object", "properties": {
            "full": {"type": "boolean", "description": "Return complete block content instead of just id/type."}}},
    }},
    {"type": "function", "function": {
        "name": "set_document",
        "description": "Replace the entire draft document with a new list of blocks.",
        "parameters": {"type": "object", "properties": {
            "blocks": {"type": "array", "items": {"type": "object"}, "description": _BLOCK_DESC}},
            "required": ["blocks"]},
    }},
    {"type": "function", "function": {
        "name": "add_block",
        "description": "Append or insert a single block into the draft.",
        "parameters": {"type": "object", "properties": {
            "block": {"type": "object", "description": _BLOCK_DESC},
            "index": {"type": "integer", "description": "Insert position; omit to append."}},
            "required": ["block"]},
    }},
    {"type": "function", "function": {
        "name": "update_block",
        "description": "Update fields of an existing block by id (surgical edit).",
        "parameters": {"type": "object", "properties": {
            "block_id": {"type": "string"},
            "fields": {"type": "object", "description": "Fields to merge into the block."}},
            "required": ["block_id", "fields"]},
    }},
    {"type": "function", "function": {
        "name": "remove_block",
        "description": "Remove a block by id.",
        "parameters": {"type": "object", "properties": {
            "block_id": {"type": "string"}}, "required": ["block_id"]},
    }},
    {"type": "function", "function": {
        "name": "move_block",
        "description": "Move a block to a new position.",
        "parameters": {"type": "object", "properties": {
            "block_id": {"type": "string"}, "to_index": {"type": "integer"}},
            "required": ["block_id", "to_index"]},
    }},
    {"type": "function", "function": {
        "name": "read_memory",
        "description": "Read a memory file: interests, content_config, or history.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(_WRITABLE_KINDS)}}, "required": ["kind"]},
    }},
    {"type": "function", "function": {
        "name": "write_memory",
        "description": "Replace a memory file (interests, content_config, history). Use to consolidate lasting feedback.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(_WRITABLE_KINDS)},
            "content": {"type": "string"}}, "required": ["kind", "content"]},
    }},
    {"type": "function", "function": {
        "name": "append_history",
        "description": "Append a brief note to HISTORY.md for future trend-spotting.",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string"}}, "required": ["note"]},
    }},
    {"type": "function", "function": {
        "name": "write_headlines",
        "description": "Record brief notes on the items covered in THIS edition (one per line), to avoid duplicate reporting later. Call once near the end.",
        "parameters": {"type": "object", "properties": {
            "notes": {"type": "string"}}, "required": ["notes"]},
    }},
]


# ── Review-mode tool set ────────────────────────────────────────────────────
#
# A review edition reads editions rather than news items, so it swaps the data
# tools entirely. get_edition is deliberately absent: it returns whole stored
# documents, which would defeat the digest that keeps a whole period in view.
# write_headlines is absent too — the next review receives this one's full
# document, so per-edition notes would add nothing.

_REVIEW_DATA_TOOLS = [
    {"type": "function", "function": {
        "name": "list_editions_in_scope",
        "description": (
            "List every edition in the period under review: its headline, "
            "subheader, and the headline of each featured story with the "
            "block_id you need to open it."
        ),
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_edition_item",
        "description": (
            "Full text of ONE featured story from a past edition — headline, "
            "subheader, body and sources. Call this for every story you intend "
            "to write about; a headline alone is not enough to describe an arc."
        ),
        "parameters": {"type": "object", "properties": {
            "run_id": {"type": "integer"},
            "block_id": {"type": "string"}},
            "required": ["run_id", "block_id"]},
    }},
]

_REVIEW_EXCLUDED = {
    "list_scope_items", "get_item", "list_past_editions", "get_edition",
    "read_headlines", "read_coverage", "write_headlines", "append_history",
}

REVIEW_TOOL_SPECS = _REVIEW_DATA_TOOLS + [
    spec for spec in TOOL_SPECS
    if spec["function"]["name"] not in _REVIEW_EXCLUDED
]
