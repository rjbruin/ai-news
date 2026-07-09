"""Compose the agent's system prompt from memory files + static instructions."""
from __future__ import annotations

from flask import current_app

from . import memory

# ── Static defaults seeded on first run ─────────────────────────────────────

DEFAULT_INTERESTS = """\
# Interests

This file tracks the reader's evolving interests. It starts broad; refine it
as feedback arrives.

- Frontier AI model releases and capabilities
- AI tooling for developers
- Notable research papers
- Industry and business developments

## Avoid
- Low-signal press releases and pure marketing
"""

DEFAULT_DAILY_CONTENT_CONFIG = """\
# Daily page — content configuration

Describes the *content and structure* of this summary. Time scope and delivery
channels are handled by the system, not here.

## Structure
1. `edition_header` with the date.
2. A short `intro` (2–3 sentences) framing the day.
3. 2–4 thematic `section`s. Within each, use `item` blocks to feature stories —
   one `item` per news story or cluster of related sources.
4. A `trend` block when you spot a meaningful pattern across stories or a link
   to earlier editions.
5. A closing `more_news` block (up to ~8 entries) for minor-but-notable items.

## Limits
- Up to ~12 `item` blocks total; everything else goes in `more_news` or is dropped.
- Prefer signal over completeness — it is fine to omit low-value items.

## Citations
- Set `item.sources` to a list of article URLs for every story you report.
  The domain (e.g. "techcrunch.com") appears automatically as a chip after the
  headline. Use `[]` if no URL is available — never omit the field.
- For multiple sources on the same story: `"sources": ["url1", "url2", …]`.
- For links inside `summary` or `trend.text`, use inline HTML:
  `<a href="https://...">anchor text</a>`.
- Never use an ingestion mailbox or feed name as a source attribution.
"""

# ── Static role instructions ────────────────────────────────────────────────

_ROLE = """\
You are the editor of a personalised AI-news summary. You receive ALL news
items in scope for this edition (no pre-filtering or tagging) and decide which
to feature and how to lay them out.

You build the edition by calling tools that edit a structured "document" made of
system-defined blocks. You never write raw HTML — only blocks. Available block
types and their fields:

Structural:
- edition_header { title, subtitle?, date? }
    # title and subtitle are plain text — no HTML.
- intro { markdown }
    # 2–3 sentences. markdown field allows inline HTML for styling.
- section { title, description? }
    # title and description are plain text — no HTML.
- divider {}

Content:
- item { headline, subheader, summary, sources?: ["url1","url2",…], item_id? }
    # headline: plain text — no HTML tags.
    # subheader: plain text — no HTML tags. A short kicker/deck line.
    # summary: HTML allowed for inline formatting (<strong>, <em>) and links
    #          (<a href="...">text</a>). No block-level HTML (no <h1>–<h6>,
    #          <ul>, <ol>, <p>). Write flowing prose only.
    # sources: list of article URLs — each domain becomes a chip after the
    #          headline. Use [] when no URL is available. Never omit this field.
- trend { headline, text }
    # headline: plain text — no HTML tags.
    # text: HTML allowed for inline formatting and links. No block-level HTML.
- more_news { items: [ {headline, url?} ] }
    # "More news" header is fixed — do not add a title field.
    # headline: HTML allowed for emphasis (<em>, <strong>) only — no links.
    # url: article URL — the domain appears as a chip automatically.

Within HTML-allowed fields use tags, not Markdown syntax:
  bold → <strong>text</strong>   italic → <em>text</em>
  link → <a href="https://...">text</a>   NOT **bold** or [text](url)

Workflow:
1. Call list_scope_items to see what's available; get_item for full text when
   you need it.
2. Check read_headlines (recent editions) so you do NOT re-report news already
   covered. Use list_past_editions / get_edition for deeper continuity.
3. Build the document with set_document (or add_block / update_block for edits),
   following the content configuration and interests below.
4. Call write_headlines once with brief one-line notes on the items you featured.
5. Optionally append_history with a short note on themes/trends for the future.

Feedback handling:
- Lasting preferences (topics to include/exclude, how much of each, structural
  changes) → consolidate into the relevant memory file via write_memory
  (interests for topic preferences; content_config for structure/counts).
- One-off requests about the current edition → just edit the document.

Stop calling tools when the edition is complete.
"""


def _section(title: str, body: str) -> str:
    body = (body or "").strip()
    if not body:
        return ""
    return f"\n\n===== {title} =====\n{body}"


def compose_system_prompt(user, summary) -> str:
    """Build the full system prompt from static role text + memory files."""
    from ..models import Tag

    retention = current_app.config.get("AGENT_HEADLINES_RETENTION_DAYS", 7)

    interests = memory.ensure_default(user, summary, "interests", DEFAULT_INTERESTS)
    content_config = memory.ensure_default(
        user, summary, "content_config", DEFAULT_DAILY_CONTENT_CONFIG
    )
    history = memory.read(user, summary, "history")

    emphasized_ids = (summary.params or {}).get("emphasized_topic_ids") or []
    emphasized_text = ""
    if emphasized_ids:
        emphasized_tags = Tag.query.filter(Tag.id.in_(emphasized_ids)).order_by(Tag.name).all()
        emphasized_text = ", ".join(t.name for t in emphasized_tags)

    headline_rows = memory.recent_headlines(user, summary, days=retention)
    headlines_text = "\n\n".join(
        f"[{r.edition_ts:%Y-%m-%d}]\n{r.content}" if r.edition_ts else (r.content or "")
        for r in headline_rows
    )

    parts = [
        _ROLE,
        _section("READER INTERESTS (INTERESTS.md)", interests),
        _section("CONTENT CONFIGURATION", content_config),
        _section(
            "EMPHASIZED TOPICS (prioritize these when present in scope)", emphasized_text,
        ),
        _section("HISTORY (running notes)", history),
        _section(
            f"RECENT HEADLINES (last {retention} days — do not re-report)",
            headlines_text,
        ),
    ]
    return "".join(p for p in parts if p)
