"""Background scheduler (APScheduler) — periodic source polling + tagging.

Runs in-process. A guard prevents double-start under the Flask reloader.
The poll job itself checks each source's interval, so we tick frequently and
let ``ingest_all_due`` decide what is actually due.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def start_scheduler(app: Flask) -> BackgroundScheduler | None:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    # Tick at most once a minute; ingest_all_due enforces real intervals.
    tick = min(60, app.config.get("POLL_INTERVAL", 3600))

    scheduler = BackgroundScheduler(daemon=True, timezone="UTC")

    def _poll_job():
        with app.app_context():
            from ..services import ingest

            try:
                totals = ingest.ingest_all_due(force=False)
                if totals["sources"]:
                    logger.info("Scheduled poll: %s", totals)
            except Exception:  # noqa: BLE001
                logger.exception("Scheduled poll failed")

    scheduler.add_job(
        _poll_job,
        "interval",
        seconds=tick,
        id="poll_sources",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("Scheduler started (tick=%ss)", tick)
    return scheduler
