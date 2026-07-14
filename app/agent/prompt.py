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
- Set `item.item_id` to the id of the news item you're citing — the system
  fills in `sources` with that item's real URL automatically. Don't type
  `sources` by hand for a single-item story; the system's URL always wins
  over anything typed there.
- Only set `sources` yourself for a story spanning multiple items with no
  single `item_id` to point at — a list of article URLs, e.g.
  `"sources": ["url1", "url2", …]`. Use `[]` if no URL is available.
- For links inside `summary` or `trend.text`, use inline HTML:
  `<a href="https://...">anchor text</a>`.
- Never use an ingestion mailbox or feed name as a source attribution.
"""

# ── Static role instructions ────────────────────────────────────────────────

_ROLE = """\
You are the editor of a personalised AI-news summary. You receive ALL news
items in scope for this edition (no pre-filtering) — each item includes any
Topics it's been classified under, as a hint for grouping into sections — but
you still decide what to feature and how to lay it out.

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
    # Prefer a name from AVAILABLE TOPICS (below) when one fits; invent your
    # own when nothing there fits the day's stories.
- divider {}

Content:
- item { headline, subheader, summary, item_id?, sources?: ["url1","url2",…] }
    # headline: plain text — no HTML tags.
    # subheader: plain text — no HTML tags. A short kicker/deck line.
    # summary: HTML allowed for inline formatting (<strong>, <em>) and links
    #          (<a href="...">text</a>). No block-level HTML (no <h1>–<h6>,
    #          <ul>, <ol>, <p>). Write flowing prose only.
    # item_id: the id of the news item this story is about — set this and
    #          the system fills in sources with that item's real URL
    #          automatically (overriding anything you put in sources). Only
    #          fill sources by hand for a multi-item story with no single
    #          item_id — a list of article URLs, each becoming a domain chip
    #          after the headline.
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
3. Decide the full structure and story list, THEN build it in ONE
   set_document call, following the content configuration and interests
   below. Do not construct the document by adding blocks one at a time —
   every tool call re-sends the whole growing conversation, so building
   block-by-block costs far more than composing the draft up front and
   submitting it in a single set_document. Use add_block / update_block
   only for small later adjustments to an already-submitted document, not
   for initial construction.
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
    from ..extensions import db
    from ..models import Tag

    retention = current_app.config.get("AGENT_HEADLINES_RETENTION_DAYS", 7)

    interests = memory.ensure_default(user, summary, "interests", DEFAULT_INTERESTS)
    content_config = memory.ensure_default(
        user, summary, "content_config", DEFAULT_DAILY_CONTENT_CONFIG
    )
    history = memory.read(user, summary, "history")

    topics = (
        Tag.query.filter(
            Tag.archived_at.is_(None),
            db.or_(Tag.scope == "global", Tag.owner_user_id == user.id),
        ).order_by(Tag.name).all()
    )
    topics_text = ", ".join(t.name for t in topics)

    tiers = (summary.params or {}).get("topic_tiers") or {}
    highlight_ids = tiers.get("highlights") or []
    none_ids = tiers.get("none") or []
    emphasis_lines = []
    if highlight_ids or none_ids:
        tags_by_id = {
            t.id: t.name
            for t in Tag.query.filter(Tag.id.in_(highlight_ids + none_ids)).all()
        }
        highlight_names = sorted(tags_by_id[i] for i in highlight_ids if i in tags_by_id)
        none_names = sorted(tags_by_id[i] for i in none_ids if i in tags_by_id)
        if highlight_names:
            emphasis_lines.append(
                "Cover only as brief highlights (not full stories): " + ", ".join(highlight_names)
            )
        if none_names:
            emphasis_lines.append(
                "Do not emphasize; include only if clearly newsworthy on its own: "
                + ", ".join(none_names)
            )
    emphasis_text = "\n".join(emphasis_lines)

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
            "AVAILABLE TOPICS",
            (
                "Suggested vocabulary for section titles — use these where they fit "
                "the day's stories, but don't force a story into one, and invent "
                "your own section title when nothing here fits:\n" + topics_text
            ) if topics_text else "",
        ),
        _section("TOPIC EMPHASIS", emphasis_text),
        _section("HISTORY (running notes)", history),
        _section(
            f"RECENT HEADLINES (last {retention} days — do not re-report)",
            headlines_text,
        ),
    ]
    return "".join(p for p in parts if p)
