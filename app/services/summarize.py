"""Resolve a Summary config's item scope, build its artifact, and cut editions."""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import current_app

from ..extensions import db
from ..models import NewsItem, Summary, SummaryRun, utcnow
from ..summaries import registry as summary_registry

logger = logging.getLogger(__name__)


# ─────────────────────────── range resolution ────────────────────────────

def resolve_range(summary: Summary) -> tuple[datetime | None, datetime]:
    """Compute (start, end) UTC datetimes for a summary's in-scope window."""
    now = utcnow()
    params = summary.params or {}

    # Explicit override from params (used by debug_window type)
    rs = params.get("range_start")
    re = params.get("range_end")
    if rs and re:
        try:
            start = datetime.fromisoformat(rs).replace(tzinfo=timezone.utc)
            end = datetime.fromisoformat(re).replace(tzinfo=timezone.utc)
            return start, end
        except (ValueError, TypeError):
            pass

    if summary.scope_mode == "since_last":
        start = summary.last_consumed_at
        if start and start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return start, now

    if summary.period == "week":
        release_day = int(params.get("release_day", "0"))  # 0=Monday
        days_since = (now.weekday() - release_day) % 7
        cutoff = now.replace(hour=8, minute=0, second=0, microsecond=0) - timedelta(days=days_since)
        if now < cutoff:
            cutoff -= timedelta(weeks=1)
        return cutoff - timedelta(weeks=1), cutoff

    # daily
    release_time = params.get("release_time", "08:00")
    try:
        h, m = map(int, release_time.split(":"))
    except (ValueError, AttributeError):
        h, m = 8, 0
    cutoff = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now < cutoff:
        cutoff -= timedelta(days=1)
    return cutoff - timedelta(days=1), cutoff


def _edition_label(summary: Summary, range_start: datetime | None, range_end: datetime, generated_at: datetime) -> str:
    params = summary.params or {}
    if params.get("range_start") and params.get("range_end") and range_start:
        # Debug window: show the explicit range
        return f"{range_start.strftime('%-d %b %H:%M')} – {range_end.strftime('%-d %b %H:%M')}"
    if summary.scope_mode == "since_last":
        return generated_at.strftime("%-d %B – %H:%M")
    if summary.period == "week":
        if range_start:
            start_m = range_start.strftime("%B")
            end_m = range_end.strftime("%B")
            if start_m == end_m:
                return f"{range_start.strftime('%B %-d')} – {range_end.strftime('%-d')}"
            return f"{range_start.strftime('%B %-d')} – {range_end.strftime('%B %-d')}"
        return range_end.strftime("Week of %B %-d")
    # daily
    return range_end.strftime("%A %B %-d")


# ─────────────────────────── item scoping ────────────────────────────

def items_in_scope(summary: Summary) -> list[NewsItem]:
    start, end = resolve_range(summary)
    q = NewsItem.query
    if start is not None:
        q = q.filter(NewsItem.fetched_at >= start.replace(tzinfo=None))
    q = q.filter(NewsItem.fetched_at <= end.replace(tzinfo=None))
    return q.order_by(NewsItem.fetched_at.desc()).all()


# ─────────────────────────── building ────────────────────────────

def build_summary(summary: Summary, *, record_run: bool = True, mark_consumed: bool = False):
    """Build the artifact for a summary config and optionally persist it."""
    plugin = summary_registry.create(summary.type_key)
    if plugin is None:
        raise ValueError(f"Unknown summary type: {summary.type_key}")

    start, end = resolve_range(summary)
    items = items_in_scope(summary)
    artifact = plugin.build(
        items, summary.params or {}, range_start=start, range_end=end
    )

    run = None
    if record_run:
        now = utcnow()
        label = _edition_label(summary, start, end, now)
        run = SummaryRun(
            summary_id=summary.id,
            range_start=start.replace(tzinfo=None) if start else None,
            range_end=end.replace(tzinfo=None),
            item_count=len(items),
            label=label,
            content=artifact.html,
            artifact_ref=artifact.file_path,
            status="ok",
        )
        db.session.add(run)

    if mark_consumed:
        summary.last_consumed_at = utcnow()
    db.session.commit()
    return artifact, items, run


# ─────────────────────────── scheduled edition cutting ────────────────────────────

def cut_due_editions() -> int:
    """Cut new editions for all fixed_period summaries whose release time has arrived.

    Returns the number of editions cut.
    """
    cut = 0
    for summary in Summary.query.filter_by(scope_mode="fixed_period", enabled=True).all():
        try:
            _, expected_end = resolve_range(summary)
            latest = (
                SummaryRun.query
                .filter_by(summary_id=summary.id)
                .order_by(SummaryRun.range_end.desc())
                .first()
            )
            expected_naive = expected_end.replace(tzinfo=None)
            if latest and latest.range_end and latest.range_end >= expected_naive:
                continue  # Edition already exists for this period

            now = utcnow()
            # Only cut if the cutoff time has actually passed
            if now.replace(tzinfo=None) < expected_naive:
                continue

            artifact, items, run = build_summary(summary, record_run=True)
            cut += 1
            logger.info("Cut edition '%s' for summary %d", run.label if run else "?", summary.id)

            if summary.params and summary.params.get("send_email") and run:
                try:
                    _send_edition_email(summary, run, artifact)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to send email for edition %d", run.id)

        except Exception:  # noqa: BLE001
            logger.exception("Failed to cut edition for summary %d", summary.id)

    return cut


# ─────────────────────────── email sending ────────────────────────────

def _send_edition_email(summary: Summary, run: SummaryRun, artifact) -> None:
    cfg = current_app.config
    smtp_host = cfg.get("SMTP_HOST", "")
    if not smtp_host:
        logger.warning("SMTP not configured; skipping email for summary %d", summary.id)
        return

    user = summary.user
    if not user or not user.email:
        return

    subject = f"{summary.name} – {run.label or 'Edition'}"
    html_body = artifact.html or ""

    full_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{subject}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; max-width: 700px; margin: 0 auto; padding: 16px; color: #212529; }}
  a {{ color: #1f3a5f; }}
  h2 {{ color: #1f3a5f; }}
</style>
</head>
<body>
<p style="color:#6c757d;font-size:.875rem;margin-bottom:1.5rem">{summary.name} · {run.label or ''}</p>
{html_body}
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.get("SMTP_USERNAME", "noreply@ainews")
    msg["To"] = user.email
    msg.attach(MIMEText(full_html, "html", "utf-8"))

    use_tls = cfg.get("SMTP_USE_TLS", True)
    port = cfg.get("SMTP_PORT", 587)
    username = cfg.get("SMTP_USERNAME", "")
    password = cfg.get("SMTP_PASSWORD", "")

    with smtplib.SMTP(smtp_host, port) as s:
        if use_tls:
            s.starttls()
        if username:
            s.login(username, password)
        s.sendmail(msg["From"], [user.email], msg.as_string())

    logger.info("Sent edition email '%s' to %s", subject, user.email)
