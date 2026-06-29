"""File-like accessor over the AgentMemory table.

Presents the agent's memory as named Markdown "files" while physically storing
them in the DB (multi-server safe). Singleton kinds (interests, content_config,
history) have one row per scope; headlines have one row per edition.
"""
from __future__ import annotations

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
