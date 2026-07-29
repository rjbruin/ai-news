"""Condenses a period's editions into the input for a review edition.

A review looks back over *editions*, not raw news items, and handing the review
editor whole editions would blow its context on prose it mostly does not need.
So each edition contributes only its skeleton: the edition headline and
subheader, plus the headline of every featured item. Item subheaders, item
bodies and quick hits are excluded — the editor pulls the full text of a single
item on demand, via the get_edition_item tool.

The digest is *derived from the stored block document* rather than written by
the original editor at generation time. Every field it needs already exists
there, it works retroactively for editions published before reviews existed,
and it cannot drift from what was actually published — the failure mode
docs/story-dedup-spec.md records for editor-written prose notes.

Measured on real production data: 14 editions with 180 featured items condense
to ~222 lines / ~5k tokens, which is affordable at month scale. A year is not,
hence the line budget below.
"""
from __future__ import annotations

import logging
from datetime import datetime

from .summarize import edition_heads

logger = logging.getLogger(__name__)

# Above this many featured-item lines, editions are summarised by headline
# only. A yearly review over ~250 editions would otherwise reach ~90k tokens.
DEFAULT_MAX_ITEM_LINES = 400

# Block types that count as a featured story. `story` and `cluster` are the
# pre-`item` generation (see blocks.py's legacy section) and are still present
# in stored documents — 3 of the 14 July 2026 editions use them, carrying 33
# stories between them. Matching on `item` alone silently drops ~15% of that
# month from the digest, which the reviewer would have no way to notice.
FEATURED_BLOCK_TYPES = ("item", "story", "cluster")


def edition_digest(run) -> dict:
    """The skeleton of one edition: its header plus its featured item headlines.

    Featured items are the blocks with a headline and a body (see
    FEATURED_BLOCK_TYPES). ``more_news``/``quick_hits`` are deliberately
    excluded: a long tail of one-liners would dominate the digest without
    telling the reviewer much about what the period was actually about.
    """
    document = run.document or []
    header = next((b for b in document if b.get("type") == "edition_header"), {})
    items = [
        {"block_id": b.get("id"), "headline": (b.get("headline") or "").strip()}
        for b in document
        if b.get("type") in FEATURED_BLOCK_TYPES and (b.get("headline") or "").strip()
    ]
    return {
        "run_id": run.id,
        "date": run.generated_at.date().isoformat() if run.generated_at else None,
        "label": run.label,
        "headline": (header.get("title") or run.headline or "").strip(),
        "subheader": (header.get("subtitle") or "").strip(),
        "items": items,
    }


def digest_for_range(
    summary, start: datetime, end: datetime, *, max_item_lines: int | None = None,
) -> list[dict]:
    """Digests of every edition of ``summary`` generated in [start, end).

    Reviews are excluded — a review reviews editions, not other reviews.
    Oldest first, so the editor reads the period as it unfolded.

    When the period holds more featured items than ``max_item_lines``, item
    lines are dropped and only the per-edition headers are kept. That is
    reported in the returned dicts (``items_omitted``) and logged, because a
    silently trimmed digest reads to the editor as a complete record.
    """
    if max_item_lines is None:
        max_item_lines = DEFAULT_MAX_ITEM_LINES

    # generated_at is stored naive-UTC (SQLite drops tzinfo), while callers
    # reasonably hand us aware bounds — resolve_review_range returns them, as
    # does the CLI. Normalise rather than making every caller remember.
    start = start.replace(tzinfo=None) if start.tzinfo else start
    end = end.replace(tzinfo=None) if end.tzinfo else end

    runs = [
        r for r in edition_heads(summary, kind="edition")
        if r.generated_at is not None
        and start <= r.generated_at < end
        and r.document
    ]
    runs.sort(key=lambda r: r.generated_at)

    digests = [edition_digest(r) for r in runs]
    total_items = sum(len(d["items"]) for d in digests)

    if total_items > max_item_lines:
        logger.info(
            "Review digest for summary %s: %d featured items across %d editions "
            "exceeds the %d-line budget — keeping edition headers only.",
            summary.id, total_items, len(digests), max_item_lines,
        )
        for d in digests:
            d["items_omitted"] = len(d["items"])
            d["items"] = []

    return digests


def render_digest(digests: list[dict]) -> str:
    """The digest as the plain text block the review editor is handed."""
    if not digests:
        return "(no editions in this period)"

    lines: list[str] = []
    for d in digests:
        lines.append(f"--- edition {d['run_id']} · {d['date']} ({d['label'] or ''}) ---")
        if d["headline"]:
            lines.append(f"HEADLINE: {d['headline']}")
        if d["subheader"]:
            lines.append(f"SUBHEAD:  {d['subheader']}")
        for item in d["items"]:
            lines.append(f"  [{item['block_id']}] {item['headline']}")
        if d.get("items_omitted"):
            lines.append(
                f"  ({d['items_omitted']} featured items omitted — this period is "
                f"too large to list them all; use get_edition_item on a run_id "
                f"you want to open up)"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def find_item_block(run, block_id: str) -> dict | None:
    """One featured item block from a stored edition, by block id."""
    for block in run.document or []:
        if block.get("type") in FEATURED_BLOCK_TYPES and block.get("id") == block_id:
            return block
    return None


def item_block_payload(block: dict) -> dict:
    """A block flattened to a stable shape, whichever generation wrote it.

    Modern ``item`` blocks carry subheader/summary; the legacy ``story`` and
    ``cluster`` blocks carry dek/body for the same two things. Normalising here
    means the review editor sees one contract instead of having to know which
    era an edition came from.
    """
    sources = block.get("sources") or block.get("urls") or []
    if not sources and block.get("url"):
        sources = [block["url"]]
    return {
        "block_id": block.get("id"),
        "headline": (block.get("headline") or "").strip(),
        "subheader": (block.get("subheader") or block.get("dek") or "").strip(),
        "text": (block.get("summary") or block.get("body") or "").strip(),
        "sources": sources,
    }
