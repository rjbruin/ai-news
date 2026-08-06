"""Ask Dispatch — answer a question about the news with enforced citations.

A read-only agent loop over the news pool and a Dispatch's own past editions,
run on the asking user's OpenRouter key. It is deliberately *not* the editor:
it writes no document, edits no memory, and can only read.

**Citations are never model-authored URLs.** The model cites by referring to
things it has actually been shown, using markers:

    [item:123]        a NewsItem in the system
    [run:45]          a past edition of this Dispatch
    [run:45#b_9f2a]   a specific story inside that edition

``render_answer`` resolves every marker against the database and rewrites it
into a real link. Anything that doesn't resolve is replaced with a visible
"unverified reference" marker rather than silently dropped, so a hallucinated
citation is surfaced to the reader instead of quietly disappearing. A model
that writes a bare URL gets no link out of it at all — only markers become
links, so there is no path by which an invented URL reaches the reader as
something clickable.

This is enforcement of *provenance*, not of truthfulness: it guarantees every
citation points at something really in the system and really says what the
link says, and it guarantees the answer names its sources. It cannot
guarantee the surrounding sentence faithfully characterises the cited item.
"""
from __future__ import annotations

import json
import logging
import re

from flask import url_for
from markupsafe import Markup, escape

from ..extensions import db
from ..models import NewsItem, SummaryRun

logger = logging.getLogger(__name__)

MAX_STEPS = 8
# Cap what any one tool call can pour into the context — a broad query
# shouldn't be able to blow the budget in a single step.
MAX_SEARCH_RESULTS = 25

_CITE_RE = re.compile(r"\[(item|run):(\d+)(?:#([A-Za-z0-9_\-:]+))?\]")


SYSTEM_PROMPT = """\
You answer questions about the news, for a reader of the Dispatch called
'{dispatch}'. You are a research assistant over a news archive — not the
editor. You cannot change anything; you can only look things up and answer.

You have two bodies of material:
- The shared pool of ingested news items (search_news, get_news_item).
- This Dispatch's own past editions (search_editions, get_edition).

CITATIONS ARE MANDATORY.
Every factual claim in your answer must carry a citation to something you
actually retrieved in this conversation. Cite with these exact markers:

    [item:<id>]            a news item, by its id
    [run:<id>]             a past edition, by its id
    [run:<id>#<block id>]  a specific story within an edition

The system turns each marker into a real link. Rules:
- NEVER write a URL yourself. A raw URL you type is not a citation and will
  not be linked. Only markers become links.
- Only cite ids you have actually seen in a tool result in this
  conversation. Do not guess an id, and do not cite something you did not
  retrieve — an unresolvable citation is shown to the reader as broken.
- Put the citation immediately after the claim it supports.
- If the archive does not support an answer, say so plainly. "I don't have
  anything on that" is a correct and useful answer. Do not fill the gap
  with knowledge from your training data — you are answering *from this
  archive*, and an uncited claim is worse than no claim.

Style: answer directly and concisely, in prose or short bullets. Lead with
the answer, not with a description of your search. Use Markdown for
emphasis and lists. Do not include a "Sources" section — the inline
citations are the sources.
"""


# ── Tools ────────────────────────────────────────────────────────────────────

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": (
                "Full-text search over ingested news items (title, summary, "
                "one-liner). Returns matching items with their ids."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "description": "Default 15, max 25."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_news_item",
            "description": "Full stored text of one news item by id.",
            "parameters": {
                "type": "object",
                "properties": {"item_id": {"type": "integer"}},
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_editions",
            "description": (
                "Search this Dispatch's past editions. Returns matching "
                "editions with their run ids and the ids of the story blocks "
                "that matched, which you can cite as [run:<id>#<block id>]."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "description": "Default 8, max 25."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_edition",
            "description": "Full text of one past edition of this Dispatch, block by block.",
            "parameters": {
                "type": "object",
                "properties": {"run_id": {"type": "integer"}},
                "required": ["run_id"],
            },
        },
    },
]


def _clamp(limit, default, ceiling=MAX_SEARCH_RESULTS):
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(ceiling, n))


def _news_brief(item: NewsItem) -> dict:
    when = item.published_at or item.fetched_at
    return {
        "item_id": item.id,
        "cite_as": f"[item:{item.id}]",
        "title": item.title,
        "one_liner": item.one_liner,
        "date": when.strftime("%Y-%m-%d") if when else None,
    }


def _block_text(block: dict) -> str:
    """Flatten one block to plain text for search/reading."""
    parts = [
        block.get(f) or ""
        for f in ("title", "subtitle", "headline", "subheader", "summary",
                  "markdown", "text", "description", "body", "dek")
    ]
    for entry in block.get("items") or []:
        if isinstance(entry, dict):
            parts.append(entry.get("headline") or entry.get("text") or "")
    return re.sub(r"<[^>]+>", " ", " ".join(p for p in parts if p)).strip()


def _run_tool(name: str, args: dict, summary) -> str:
    if name == "search_news":
        query = (args.get("query") or "").strip()
        if not query:
            return json.dumps({"error": "query is required"})
        like = f"%{query}%"
        items = (
            NewsItem.query
            .filter(db.or_(
                NewsItem.title.ilike(like),
                NewsItem.summary_text.ilike(like),
                NewsItem.one_liner.ilike(like),
            ))
            .order_by(NewsItem.fetched_at.desc())
            .limit(_clamp(args.get("limit"), 15))
            .all()
        )
        return json.dumps({"count": len(items), "items": [_news_brief(i) for i in items]})

    if name == "get_news_item":
        item = db.session.get(NewsItem, args.get("item_id") or 0)
        if item is None:
            return json.dumps({"error": "No such news item."})
        d = _news_brief(item)
        d["summary_text"] = item.summary_text
        d["url_domain"] = (item.url or "").split("/")[2] if item.url else None
        return json.dumps(d)

    if name == "search_editions":
        query = (args.get("query") or "").strip().lower()
        if not query:
            return json.dumps({"error": "query is required"})
        runs = (
            SummaryRun.query
            .filter_by(summary_id=summary.id, status="ok")
            .filter(SummaryRun.document.isnot(None))
            .order_by(SummaryRun.generated_at.desc())
            .limit(200)
            .all()
        )
        hits = []
        for run in runs:
            matched = [
                {"block_id": b.get("id"),
                 "cite_as": f"[run:{run.id}#{b.get('id')}]",
                 "headline": b.get("headline") or b.get("title") or ""}
                for b in (run.document or [])
                if query in _block_text(b).lower()
            ]
            if matched:
                hits.append({
                    "run_id": run.id,
                    "cite_as": f"[run:{run.id}]",
                    "label": run.label,
                    "headline": run.headline,
                    "date": run.generated_at.strftime("%Y-%m-%d") if run.generated_at else None,
                    "matching_blocks": matched[:10],
                })
            if len(hits) >= _clamp(args.get("limit"), 8):
                break
        return json.dumps({"count": len(hits), "editions": hits})

    if name == "get_edition":
        run = db.session.get(SummaryRun, args.get("run_id") or 0)
        # Scoped to this Dispatch on purpose — Ask Dispatch must not become a
        # way to read another user's unpublished editions.
        if run is None or run.summary_id != summary.id:
            return json.dumps({"error": "No such edition in this Dispatch."})
        return json.dumps({
            "run_id": run.id,
            "cite_as": f"[run:{run.id}]",
            "label": run.label,
            "date": run.generated_at.strftime("%Y-%m-%d") if run.generated_at else None,
            "blocks": [
                {"block_id": b.get("id"), "type": b.get("type"),
                 "cite_as": f"[run:{run.id}#{b.get('id')}]",
                 "text": _block_text(b)}
                for b in (run.document or [])
            ],
        })

    return json.dumps({"error": f"Unknown tool {name}"})


# ── Answer rendering ─────────────────────────────────────────────────────────

def _resolve_citation(kind: str, ident: int, block_id: str | None, summary):
    """(label, url) for a citation marker, or None if it doesn't resolve."""
    if kind == "item":
        item = db.session.get(NewsItem, ident)
        if item is None:
            return None
        # Prefer the article itself; fall back to the in-app news page when
        # the item has no URL (offline/newsletter-only items).
        url = item.url or url_for("web.news", q=item.title)
        return (item.title or f"item {ident}", url)

    run = db.session.get(SummaryRun, ident)
    if run is None or run.summary_id != summary.id:
        return None
    url = url_for("web.edition_view", summary_id=summary.id, run_id=run.id)
    label = run.label or (run.generated_at.strftime("%Y-%m-%d") if run.generated_at else f"edition {ident}")
    if block_id:
        block = next((b for b in (run.document or []) if b.get("id") == block_id), None)
        if block is None:
            return None
        url = f"{url}#{block_id}"
        headline = block.get("headline") or block.get("title")
        if headline:
            label = f"{headline} · {label}"
    return (label, url)


def render_answer(text: str, summary) -> tuple[Markup, list[dict]]:
    """Turn an answer containing citation markers into safe HTML plus the
    list of resolved references.

    Everything outside a marker is escaped, so no model-authored HTML (or
    model-authored ``<a href>``) can reach the page. Markers that resolve
    become numbered links; markers that don't become a visible warning, so a
    fabricated citation is obvious rather than invisible.
    """
    refs: list[dict] = []
    seen: dict[tuple, int] = {}
    out: list[str] = []
    pos = 0

    for m in _CITE_RE.finditer(text or ""):
        out.append(str(escape(text[pos:m.start()])))
        pos = m.end()
        kind, ident, block_id = m.group(1), int(m.group(2)), m.group(3)
        resolved = _resolve_citation(kind, ident, block_id, summary)
        if resolved is None:
            out.append(
                '<span class="ask-cite ask-cite--broken" '
                'title="This reference did not resolve to anything in the archive">'
                'unverified reference</span>'
            )
            continue
        label, url = resolved
        key = (kind, ident, block_id)
        if key not in seen:
            seen[key] = len(refs) + 1
            refs.append({"n": len(refs) + 1, "label": label, "url": url, "kind": kind})
        n = seen[key]
        out.append(
            f'<a class="ask-cite" href="{escape(url)}" '
            f'title="{escape(label)}" target="_blank" rel="noopener">[{n}]</a>'
        )

    out.append(str(escape(text[pos:])))
    return Markup("".join(out)), refs


# ── The loop ─────────────────────────────────────────────────────────────────

def ask(summary, question: str, *, api_key: str, model: str) -> dict:
    """Answer ``question`` about ``summary``'s news. Returns
    {"answer_html", "references", "cost", "tokens", "raw"}.
    """
    from ..llm import openrouter

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(dispatch=summary.name)},
        {"role": "user", "content": question},
    ]
    tokens = 0
    cost = 0.0
    answer = ""

    for _ in range(MAX_STEPS):
        message = openrouter.chat(messages, tools=TOOL_SPECS, api_key=api_key, model=model)
        usage = message.get("_usage") or {}
        tokens += int(usage.get("total_tokens") or 0)
        cost += float(usage.get("cost") or 0.0)

        messages.append({
            "role": "assistant",
            "content": message.get("content") or "",
            **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {}),
        })
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            answer = message.get("content") or ""
            break

        for call in tool_calls:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": _run_tool(fn.get("name", ""), args, summary),
            })
    else:
        logger.warning("Ask Dispatch hit max steps for summary %d", summary.id)
        answer = answer or (
            "I ran out of research steps before reaching an answer. "
            "Try a narrower question."
        )

    html, refs = render_answer(answer, summary)
    return {
        "answer_html": html,
        "references": refs,
        "cost": cost,
        "tokens": tokens,
        "raw": answer,
    }
