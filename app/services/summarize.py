"""Resolve a Summary config's item scope and build its artifact."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..extensions import db
from ..models import NewsItem, Summary, SummaryRun, utcnow
from ..summaries import registry as summary_registry


def resolve_range(summary: Summary) -> tuple[datetime | None, datetime]:
    """Compute (start, end) datetimes for in-scope news."""
    end = utcnow()
    if summary.scope_mode == "since_last":
        start = summary.last_consumed_at
        if start and start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return start, end
    # fixed_period
    delta = timedelta(days=7) if summary.period == "week" else timedelta(days=1)
    return end - delta, end


def items_in_scope(summary: Summary) -> list[NewsItem]:
    start, end = resolve_range(summary)
    q = NewsItem.query
    # Use fetched_at as the canonical timeline (published_at may be missing).
    if start is not None:
        q = q.filter(NewsItem.fetched_at >= start.replace(tzinfo=None))
    q = q.filter(NewsItem.fetched_at <= end.replace(tzinfo=None))
    return q.order_by(NewsItem.fetched_at.desc()).all()


def build_summary(summary: Summary, *, record_run: bool = True, mark_consumed: bool = False):
    """Build the artifact for a summary config."""
    plugin = summary_registry.create(summary.type_key)
    if plugin is None:
        raise ValueError(f"Unknown summary type: {summary.type_key}")

    start, end = resolve_range(summary)
    items = items_in_scope(summary)
    artifact = plugin.build(
        items, summary.params or {}, range_start=start, range_end=end
    )

    if record_run:
        run = SummaryRun(
            summary_id=summary.id,
            range_start=start.replace(tzinfo=None) if start else None,
            range_end=end.replace(tzinfo=None),
            item_count=len(items),
            artifact_ref=artifact.file_path,
            status="ok",
        )
        db.session.add(run)
    if mark_consumed:
        summary.last_consumed_at = utcnow()
    db.session.commit()
    return artifact, items
