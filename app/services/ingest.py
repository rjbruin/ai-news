"""Ingestion service: poll sources, extract items, persist, and tag.

Kept free of Flask request context so the scheduler can call it directly
(inside an app context).
"""
from __future__ import annotations

import logging

from ..extensions import db
from ..models import ApiKeyUsage, IngestRun, NewsItem, Source, Tag, utcnow
from ..sources import registry as source_registry
from ..tagging import engine as tagging_engine

logger = logging.getLogger(__name__)


def ingest_source(source: Source) -> dict:
    """Fetch + extract + persist + tag for a single source. Returns a stat dict."""
    stats = {"fetched": 0, "new_items": 0, "tagged": 0, "skipped": 0, "errors": 0, "error_log": []}

    api_key_row = source.api_key
    if api_key_row is None or not api_key_row.active:
        source.last_status = "error: no active API key assigned to this source"
        db.session.commit()
        return stats
    secret = api_key_row.get_key()
    if not secret:
        source.last_status = "error: assigned API key has no usable credential"
        db.session.commit()
        return stats
    model = api_key_row.resolved_model()

    usage_totals = {"tokens": 0, "cost": 0.0}

    def _usage_hook(usage: dict) -> None:
        usage_totals["tokens"] += int(usage.get("total_tokens") or 0)
        usage_totals["cost"] += float(usage.get("cost") or 0.0)

    plugin = source_registry.create(
        source.type_key, source.config or {},
        api_key=secret, model=model, usage_hook=_usage_hook,
    )
    if plugin is None:
        source.last_status = f"error: unknown source type '{source.type_key}'"
        db.session.commit()
        return stats

    try:
        docs = plugin.fetch(source.last_polled_at)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fetch failed for source %s", source.id)
        msg = f"fetch error: {exc}"
        source.last_status = msg
        source.last_polled_at = utcnow()
        db.session.commit()
        stats["errors"] += 1
        stats["error_log"].append(msg)
        return stats

    stats["fetched"] = len(docs)
    all_tags = Tag.query.all()
    new_items: list[NewsItem] = []

    for doc in docs:
        # Server-side dedup: skip documents whose external_id we have seen before.
        if doc.external_id and IngestRun.query.filter_by(
            source_id=source.id, external_id=doc.external_id
        ).first():
            stats["skipped"] += 1
            continue

        run = IngestRun(
            source_id=source.id,
            external_id=doc.external_id or None,
            subject=doc.subject,
            sender=(doc.meta or {}).get("from"),
            raw_body=doc.text,
        )
        db.session.add(run)
        db.session.flush()  # populate run.id before linking items

        try:
            extracted = plugin.extract(doc)
        except Exception as exc:  # noqa: BLE001
            msg = f"extraction error for '{doc.subject or doc.external_id}': {exc}"
            logger.exception("Extraction failed for doc %s", doc.external_id)
            stats["errors"] += 1
            stats["error_log"].append(msg)
            continue

        if not extracted:
            stats["error_log"].append(
                f"no items extracted from '{doc.subject or doc.external_id}' (LLM returned empty or failed)"
            )

        for ex in extracted:
            dedup = NewsItem.make_hash(ex.title, ex.url)
            if NewsItem.query.filter_by(dedup_hash=dedup).first():
                stats["skipped"] += 1
                continue
            item = NewsItem(
                source_id=source.id,
                ingest_run_id=run.id,
                dedup_hash=dedup,
                title=ex.title[:500],
                url=ex.url,
                summary_text=ex.summary,
                one_liner=ex.one_liner,
                item_type=ex.item_type,
                full_text=ex.full_text,
                published_at=ex.published_at,
                status="parsed",
            )
            db.session.add(item)
            new_items.append(item)

    db.session.flush()
    stats["new_items"] = len(new_items)

    for item in new_items:
        try:
            tagging_engine.apply_to_item(
                item, all_tags, api_key=secret, model=model, usage_hook=_usage_hook,
            )
            stats["tagged"] += 1
        except Exception as exc:  # noqa: BLE001
            msg = f"tagging error for '{item.title[:60]}': {exc}"
            logger.exception("Tagging failed for item %s", item.id)
            item.status = "error"
            stats["errors"] += 1
            stats["error_log"].append(msg)

    source.last_polled_at = utcnow()
    if stats["errors"]:
        status = (
            f"partial: {stats['new_items']} new / {stats['fetched']} docs / "
            f"{stats['errors']} errors / {stats['skipped']} skipped"
        )
    else:
        status = (
            f"ok: {stats['new_items']} new / {stats['fetched']} docs / "
            f"{stats['skipped']} skipped"
        )
    source.last_status = status
    if usage_totals["tokens"] or usage_totals["cost"]:
        db.session.add(ApiKeyUsage(
            api_key_id=api_key_row.id,
            source_id=source.id,
            kind="ingest",
            tokens=usage_totals["tokens"],
            cost=usage_totals["cost"],
        ))
    db.session.commit()
    return stats


def ingest_all_due(force: bool = False) -> dict:
    """Ingest every enabled source whose poll interval has elapsed."""
    from flask import current_app

    default_interval = current_app.config.get("POLL_INTERVAL", 3600)
    totals = {"sources": 0, "new_items": 0, "tagged": 0, "errors": 0}

    for source in Source.query.filter_by(enabled=True):
        interval = source.poll_interval_override or default_interval
        if not force and source.last_polled_at is not None:
            elapsed = (utcnow() - _aware(source.last_polled_at)).total_seconds()
            if elapsed < interval:
                continue
        stats = ingest_source(source)
        totals["sources"] += 1
        totals["new_items"] += stats["new_items"]
        totals["tagged"] += stats["tagged"]
        totals["errors"] += stats["errors"]
    return totals


def _aware(dt):
    """Treat naive datetimes (SQLite) as UTC."""
    from datetime import timezone

    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def retag_all() -> int:
    """Re-run tagging over all items (e.g. after taxonomy changes)."""
    tags = Tag.query.all()
    count = 0
    for item in NewsItem.query.all():
        tagging_engine.apply_to_item(item, tags)
        count += 1
    return count
