"""File-like accessor over the AgentMemory table.

Presents the agent's memory as named Markdown "files" while physically storing
them in the DB (multi-server safe). Singleton kinds (interests, content_config,
history) have one row per scope; headlines have one row per edition.
"""
from __future__ import annotations

import re
from datetime import timedelta

from ..extensions import db
from ..models import AgentMemory, utcnow

# Singleton kinds keyed by (user_id, summary_id). interests is user-level
# (summary_id NULL); the rest are per-summary.
SINGLETON_KINDS = ("interests", "content_config", "history")
USER_LEVEL_KINDS = ("interests",)


def _summary_scope(kind: str, summary) -> int | None:
    """interests is user-level; everything else is scoped to the summary."""
    if kind in USER_LEVEL_KINDS:
        return None
    return summary.id


def read(user, summary, kind: str) -> str:
    """Return the content of a singleton memory file, or '' if absent."""
    row = AgentMemory.query.filter_by(
        user_id=user.id, summary_id=_summary_scope(kind, summary), kind=kind
    ).first()
    return (row.content or "") if row else ""


def write(user, summary, kind: str, content: str) -> AgentMemory:
    """Create or replace a singleton memory file. Returns the row."""
    scope = _summary_scope(kind, summary)
    row = AgentMemory.query.filter_by(
        user_id=user.id, summary_id=scope, kind=kind
    ).first()
    if row is None:
        row = AgentMemory(user_id=user.id, summary_id=scope, kind=kind)
        db.session.add(row)
    row.content = content
    db.session.commit()
    return row


def ensure_default(user, summary, kind: str, default_content: str) -> str:
    """Seed a singleton file with default content if it doesn't exist yet."""
    existing = read(user, summary, kind)
    if existing:
        return existing
    write(user, summary, kind, default_content)
    return default_content


# ── Schema drift repair ──────────────────────────────────────────────────────

# Block types the agent used to be able to emit, before the schema moved to a
# single `item` type (see app/agent/blocks.py's AGENT_BLOCK_TYPES). A user's
# content_config is edited by the agent itself over time (write_memory
# consolidating feedback) and is never otherwise touched by the system, so it
# can silently drift out of sync with a later schema change — the agent then
# gets instructed, by its own memory, to use block types that no longer
# validate, which reliably sends it into an expensive trial-and-error loop.
_LEGACY_BLOCK_ALIASES = {
    "story": "item", "cluster": "item", "callout": "trend", "quick_hits": "more_news",
}
_LEGACY_BLOCK_PATTERN = re.compile(
    r"`(" + "|".join(re.escape(k) for k in _LEGACY_BLOCK_ALIASES) + r")`"
)


def reconcile_content_config(text: str) -> tuple[str, bool]:
    """Rewrite backtick-wrapped legacy block-type names to their current
    equivalents. Only touches code-span occurrences (`` `story` ``) to avoid
    false positives on the word appearing in prose. Returns (text, changed)."""
    changed = False

    def _sub(m: re.Match) -> str:
        nonlocal changed
        changed = True
        return f"`{_LEGACY_BLOCK_ALIASES[m.group(1)]}`"

    new_text = _LEGACY_BLOCK_PATTERN.sub(_sub, text)
    return new_text, changed


# ── Headlines (one row per edition) ────────────────────────────────────────

def write_headlines(user, summary, edition_ts, content: str) -> AgentMemory:
    """Store the HEADLINES file for one edition."""
    row = AgentMemory(
        user_id=user.id,
        summary_id=summary.id,
        kind="headlines",
        edition_ts=edition_ts,
        content=content,
    )
    db.session.add(row)
    db.session.commit()
    return row


def recent_headlines(user, summary, *, days: int) -> list[AgentMemory]:
    """Return headlines rows from the last ``days`` days, newest first."""
    floor = utcnow().replace(tzinfo=None) - timedelta(days=days)
    return (
        AgentMemory.query.filter_by(
            user_id=user.id, summary_id=summary.id, kind="headlines"
        )
        .filter(AgentMemory.edition_ts >= floor)
        .order_by(AgentMemory.edition_ts.desc())
        .all()
    )


def prune_headlines(*, days: int) -> int:
    """Delete headlines rows older than ``days`` days. Returns count removed."""
    floor = utcnow().replace(tzinfo=None) - timedelta(days=days)
    n = (
        AgentMemory.query.filter_by(kind="headlines")
        .filter(AgentMemory.edition_ts < floor)
        .delete(synchronize_session=False)
    )
    if n:
        db.session.commit()
    return n


def prune_history(*, max_chars: int) -> int:
    """Trim oversized `history` singletons to their most recent max_chars.

    `history` has no date field to prune rows by age (unlike `headlines`),
    and `append_history` never trims — left unbounded it's resent in full on
    every LLM call, forever. Cuts at the next newline after the trim point so
    a note is never split in half. Returns the number of rows trimmed.
    """
    rows = AgentMemory.query.filter_by(kind="history").filter(AgentMemory.content.isnot(None)).all()
    trimmed = 0
    for row in rows:
        content = row.content or ""
        if len(content) <= max_chars:
            continue
        cut = len(content) - max_chars
        nl = content.find("\n", cut)
        row.content = content[nl + 1:] if nl != -1 else content[-max_chars:]
        trimmed += 1
    if trimmed:
        db.session.commit()
    return trimmed
