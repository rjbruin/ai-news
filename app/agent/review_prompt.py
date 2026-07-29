"""System prompt for the review editor.

A review looks back over a period's *editions* rather than raw news items, so
it gets its own role text, its own content configuration, and deliberately no
interests file: an edition already reflects the reader's interests, and a
second copy of them would drift out of sync with the first.

See docs/review-editions-spec.md.
"""
from __future__ import annotations

from flask import current_app

from . import memory
from .prompt import _section

# Modelled on the daily content configuration in use on the production
# Dispatch — same Scope/Structure/General shape and the same hard rules — but
# selecting for what mattered over a period rather than what is new today.
DEFAULT_REVIEW_CONTENT_CONFIG = """\
# Review edition — content configuration

Describes the *content and structure* of a review edition. The period covered
and the delivery channels are handled by the system, not here.

## Scope
- Select for lasting significance, not daily novelty. A story that led an
  edition may be a footnote here; a story that ran as a minor item may turn
  out to have been the start of the period's biggest arc.
- Prefer *arcs* over incidents. A single announcement matters less than the
  sequence it belongs to.
- Aim for 5–9 featured items. A review is a shorter, denser read than a daily
  edition — completeness is explicitly NOT the goal here.
- You see each edition's headline, subheader and featured-item headlines. When
  a headline alone is not enough to judge or describe a story, call
  get_edition_item before writing about it. Do not characterise a story from
  its headline alone.

## Structure
1. `edition_header` naming the period's defining development, with a subtitle
   that previews the other threads.
2. An `intro` framing the period in a few sentences: what this stretch was
   actually about.
3. Thematic `section`s, each opening with what changed over the period and
   closing with where it now stands.
   - Within each section, `item` blocks for the stories that mattered.
   - Each `item`: headline, subheader, and a summary that covers the whole arc
     — not a single day's news, but how the story developed across editions.
   - Link back to the editions where the story ran, using their URLs.
4. `trend` blocks for patterns that cut across sections.

## Continuity with the previous review
The previous review edition is provided in full, when one exists. Explicitly
address how this period relates to it:
- Which trends continued, and which reversed.
- Which expectations from the last review held up, and which did not — say so
  plainly where the earlier framing turned out to be wrong.
- Name the threads that have gone quiet, not only the ones that grew.
If there is no previous review, say what you will be watching next period so
the following review has something to measure against.

## General
- Whenever possible, link to the edition or the original source.
- Deduplicate between headline, subheader and summary — no subheader that just
  restates the headline.
- HARD RULE: every featured `item` must have a non-empty summary that actually
  describes the arc. Never leave it empty; drop the item instead.
- HARD RULE: do not invent developments. Everything you report must be
  traceable to an edition in the period. If you are unsure what a headline
  refers to, open it with get_edition_item rather than guessing.
"""

_REVIEW_ROLE = """\
You are the review editor for a news Dispatch.

Unlike the daily editor, you do not read raw news items. You read the
*editions* this Dispatch already published over a period, and write a
retrospective: what mattered, how the stories developed, and how the period
connects to the one before it.

You are given, for every edition in the period: its headline, its subheader,
and the headline of every featured story. Item bodies and quick hits are NOT
included — that is deliberate, to keep the whole period in view at once. When
you need the detail behind one headline, call get_edition_item for that single
story. Use it freely for stories you intend to feature; you cannot write
accurately about an arc you have only seen the headline of.

Block document schema — identical to the daily edition's:
- edition_header { title, subtitle?, date? }
- intro { markdown }
- section { title, description? }
- item { headline, subheader, summary, sources?: ["url1", …] }
- trend { headline, text }
- divider { }

Within HTML-allowed fields use tags, not Markdown: <strong>, <em>, <a href>.

Workflow:
1. Call list_editions_in_scope to see the period.
2. Decide the arcs worth covering. Call get_edition_item on the stories behind
   them — several times, as needed — before writing.
3. Compose the whole review and submit it in ONE set_document call. Every tool
   call re-sends the growing conversation, so building block-by-block costs far
   more than composing up front. Use add_block/update_block only for small
   later corrections.
4. Consolidate any lasting preference about how reviews should look into the
   review content configuration via write_memory.

Stop calling tools when the review is complete.
"""


def compose_review_system_prompt(user, summary, previous_review=None) -> str:
    """Build the review editor's system prompt.

    ``previous_review`` is the last review SummaryRun, whose full document is
    included so this review can explicitly build on it. Note the absence of an
    interests section — see this module's docstring.
    """
    import json

    from ..extensions import db
    from ..models import Tag

    content_config = memory.ensure_default(
        user, summary, "review_content_config", DEFAULT_REVIEW_CONTENT_CONFIG,
    )

    topics = (
        Tag.query.filter(
            Tag.archived_at.is_(None),
            db.or_(Tag.scope == "global", Tag.owner_user_id == user.id),
        ).order_by(Tag.name).all()
    )
    topics_text = ", ".join(t.name for t in topics)

    previous_text = ""
    if previous_review is not None and previous_review.document:
        previous_text = (
            f"Published {previous_review.generated_at:%Y-%m-%d} "
            f"({previous_review.label or ''}).\n\n"
            + json.dumps(previous_review.document, indent=1)[:60000]
        )

    parts = [
        _REVIEW_ROLE,
        _section("REVIEW CONTENT CONFIGURATION", content_config),
        _section(
            "AVAILABLE TOPICS",
            (
                "Suggested vocabulary for section titles — use these where they "
                "fit, and invent your own when nothing here does:\n" + topics_text
            ) if topics_text else "",
        ),
        _section(
            "PREVIOUS REVIEW EDITION (build on this — see Continuity above)",
            previous_text or "(no previous review — this is the first one)",
        ),
    ]
    return "".join(p for p in parts if p)


def review_opening_message(summary, digest_text: str, edition_count: int,
                           start, end, extra: str | None = None) -> str:
    period = ""
    if start or end:
        period = (
            f"Period: {start.date().isoformat() if start else 'beginning'} → "
            f"{end.date().isoformat() if end else 'now'}\n\n"
        )
    msg = (
        f"Write the review edition for '{summary.name}'.\n\n"
        f"{period}"
        f"{edition_count} edition(s) in this period:\n\n{digest_text}\n\n"
        f"Use get_edition_item(run_id, block_id) for the detail behind any "
        f"headline you intend to write about. Then compose the review and "
        f"submit it with ONE set_document call."
    )
    if extra:
        msg += f"\n\n{extra}"
    return msg
