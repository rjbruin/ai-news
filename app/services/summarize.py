"""Resolve a Summary config's item scope, build its artifact, and cut editions."""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import current_app, url_for

from ..extensions import db
from ..models import NewsItem, Summary, SummaryRun, utcnow
from ..summaries import registry as summary_registry

logger = logging.getLogger(__name__)


# ─────────────────────────── range resolution ────────────────────────────

def resolve_range(summary: Summary) -> tuple[datetime | None, datetime]:
    """Compute (start, end) UTC datetimes for a summary's in-scope window."""
    now = utcnow()
    params = summary.params or {}

    # Explicit override from params (used by debug_window / debug_agentic types)
    rs = params.get("range_start")
    re = params.get("range_end")
    if rs and re:
        try:
            start = datetime.fromisoformat(rs).replace(tzinfo=timezone.utc)
            end = datetime.fromisoformat(re).replace(tzinfo=timezone.utc)
            return start, end
        except (ValueError, TypeError):
            pass

    # debug_agentic with no explicit range: return all items up to now
    from ..summaries import registry as summary_registry
    plugin = summary_registry.get(summary.type_key)
    if getattr(plugin, "type_key", None) == "debug_agentic":
        return None, now

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

    release_days_raw = params.get("release_days", [0, 1, 2, 3, 4])
    release_days = set(int(d) for d in release_days_raw) if release_days_raw else {0, 1, 2, 3, 4}

    # Walk back from now to the most recent release-day cutoff
    cutoff = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now < cutoff:
        cutoff -= timedelta(days=1)
    for _ in range(7):
        if cutoff.weekday() in release_days:
            break
        cutoff -= timedelta(days=1)

    # Start from the end of the last edition so we never miss or repeat items
    latest_run = (
        SummaryRun.query
        .filter_by(summary_id=summary.id)
        .order_by(SummaryRun.range_end.desc())
        .first()
    )
    start = None
    if latest_run and latest_run.range_end:
        start = latest_run.range_end.replace(tzinfo=timezone.utc)
    return start, cutoff


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

def items_in_window(
    start: datetime | None, end: datetime | None, *, exclude_seed: bool = False
) -> list[NewsItem]:
    """Return news items fetched within [start, end] (naive-UTC aware inputs).

    ``exclude_seed`` drops items from the "seed" debug fixture source — used
    by debug_agentic so it exercises the agent against real ingested news,
    keeping the seed fixtures available for other, dedicated test cases.
    """
    q = NewsItem.query
    if start is not None:
        q = q.filter(NewsItem.fetched_at >= start.replace(tzinfo=None))
    if end is not None:
        q = q.filter(NewsItem.fetched_at <= end.replace(tzinfo=None))
    items = q.order_by(NewsItem.fetched_at.desc()).all()
    if exclude_seed:
        items = [it for it in items if not (it.source and it.source.type_key == "seed")]
    return items


def items_in_scope(summary: Summary) -> list[NewsItem]:
    start, end = resolve_range(summary)
    return items_in_window(start, end, exclude_seed=summary.type_key == "debug_agentic")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ─────────────────────────── building ────────────────────────────

def build_summary(
    summary: Summary,
    *,
    record_run: bool = True,
    mark_consumed: bool = False,
    seed_document: list | None = None,
    extra_instruction: str | None = None,
    parent_run_id: int | None = None,
    log_fn=None,
    cancel_event=None,
):
    """Build the artifact for a summary config and optionally persist it.

    For agentic summary types the LLM agent runner produces a block document
    (using the user's OpenRouter credentials); the plugin then renders it. For
    deterministic types the plugin builds the artifact directly.

    ``seed_document`` / ``extra_instruction`` / ``parent_run_id`` support
    feedback revisions (see Phase 5).
    """
    plugin = summary_registry.create(summary.type_key)
    if plugin is None:
        raise ValueError(f"Unknown summary type: {summary.type_key}")

    start, end = resolve_range(summary)
    items = items_in_scope(summary)

    document = None
    pending_headlines = None
    agent_log: list[dict] = []
    agent_cost = None

    def _collect(event: dict) -> None:
        agent_log.append(event)
        if log_fn:
            log_fn(event)

    if getattr(plugin, "is_agentic", False):
        artifact, document, pending_headlines, agent_cost = _build_agentic(
            summary, plugin, items, start, end,
            seed_document=seed_document, extra_instruction=extra_instruction, log_fn=_collect,
            cancel_event=cancel_event,
        )
        # The agent loop only checks cancel_event between steps, so a run that
        # finishes its last step right as cancel is requested can race past
        # every in-loop check. Catch that here, before anything is persisted.
        if cancel_event is not None and cancel_event.is_set():
            from ..agent.runner import AgentCancelled
            raise AgentCancelled("Generation cancelled.")
    else:
        artifact = plugin.build(
            items, summary.params or {}, range_start=start, range_end=end
        )

    run = None
    if record_run:
        now = utcnow()
        label = _edition_label(summary, start, end, now)
        revision = 1
        if parent_run_id is not None:
            parent = db.session.get(SummaryRun, parent_run_id)
            if parent is not None:
                revision = (parent.revision or 1) + 1
        run = SummaryRun(
            summary_id=summary.id,
            range_start=start.replace(tzinfo=None) if start else None,
            range_end=end.replace(tzinfo=None),
            item_count=len(items),
            label=label,
            content=artifact.html,
            artifact_ref=artifact.file_path,
            document=document,
            agent_log=agent_log or None,
            agent_cost=agent_cost,
            parent_run_id=parent_run_id,
            revision=revision,
            status="ok",
        )
        db.session.add(run)
        db.session.flush()  # populate run.id / generated_at

        if pending_headlines:
            from ..agent import memory as agent_memory
            agent_memory.write_headlines(
                summary.user, summary, run.generated_at, pending_headlines
            )

    if mark_consumed:
        summary.last_consumed_at = utcnow()
    db.session.commit()
    return artifact, items, run


def _build_agentic(
    summary, plugin, items, start, end, *,
    seed_document, extra_instruction, log_fn=None, cancel_event=None,
):
    """Run the agent to produce a document, then render it via the plugin.

    Returns (artifact, document, pending_headlines, cost_used).
    """
    from ..agent import creds, runner
    from ..agent.context import AgentSession

    if not items and summary.type_key == "debug_agentic":
        raise ValueError(
            "No items in scope. For a debug agentic summary, set an explicit "
            "Start and End window (Edit the summary, fill in the date fields, "
            "then save) that covers items already in the system."
        )

    api_key, model = creds.resolve(summary.user)
    session = AgentSession(
        user=summary.user, summary=summary, items=items,
        range_start=start, range_end=end,
    )
    try:
        max_steps = int((summary.params or {}).get("max_steps")) or None
    except (TypeError, ValueError):
        max_steps = None
    document = runner.run_agent(
        session, api_key=api_key, model=model,
        seed_document=seed_document, extra_instruction=extra_instruction, log_fn=log_fn,
        cancel_event=cancel_event, max_steps=max_steps,
    )
    artifact = plugin.build(
        items, {**(summary.params or {}), "_document": document},
        range_start=start, range_end=end,
    )
    return artifact, document, session.pending_headlines, session.cost_used


# ─────────────────────────── feedback revisions ────────────────────────────

def _feedback_instruction(feedback: str) -> str:
    return (
        "The reader gave feedback on the previous edition, which is loaded as your "
        "current draft document. Revise the draft to address it. If the feedback "
        "expresses a LASTING preference (topics to include or exclude, how much of "
        "each, structural changes), also consolidate it into the appropriate memory "
        "file via write_memory (interests for topic preferences; content_config for "
        "structure/counts). If it is a one-off request about this edition only, just "
        f"edit the document.\n\nReader feedback:\n{feedback}"
    )


def revise_edition(parent_run: SummaryRun, feedback: str, log_fn=None, cancel_event=None) -> SummaryRun:
    """Create a new revision of an agentic edition that applies reader feedback.

    Scopes items to the parent edition's window so the revision works from the
    same material; links the new run via parent_run_id with revision bumped.
    An optional `log_fn` is called with each agent event live (in addition to
    being recorded on the new run), for streaming progress to a caller.
    """
    summary = parent_run.summary
    plugin = summary_registry.create(summary.type_key)
    if plugin is None or not getattr(plugin, "is_agentic", False):
        raise ValueError("Only agentic summaries support feedback revisions.")

    start = _aware(parent_run.range_start)
    end = _aware(parent_run.range_end) or utcnow()
    items = items_in_window(start, end, exclude_seed=summary.type_key == "debug_agentic")

    agent_log: list[dict] = []

    def _collect(event: dict) -> None:
        agent_log.append(event)
        if log_fn is not None:
            log_fn(event)

    artifact, document, _headlines, cost = _build_agentic(
        summary, plugin, items, start, end,
        seed_document=parent_run.document or [],
        extra_instruction=_feedback_instruction(feedback),
        log_fn=_collect,
        cancel_event=cancel_event,
    )
    if cancel_event is not None and cancel_event.is_set():
        from ..agent.runner import AgentCancelled
        raise AgentCancelled("Generation cancelled.")

    run = SummaryRun(
        summary_id=summary.id,
        range_start=parent_run.range_start,
        range_end=parent_run.range_end,
        item_count=len(items),
        label=parent_run.label,
        content=artifact.html,
        document=document,
        agent_log=agent_log or None,
        agent_cost=cost,
        parent_run_id=parent_run.id,
        revision=(parent_run.revision or 1) + 1,
        status="ok",
    )
    db.session.add(run)
    db.session.commit()
    return run


def revision_chain(run: SummaryRun) -> list[SummaryRun]:
    """Return all revisions of the edition ``run`` belongs to, oldest first.

    Walks to the root (parent_run_id is None) then collects all descendants.
    """
    root = run
    seen = set()
    while root.parent_run_id and root.parent_run_id not in seen:
        seen.add(root.id)
        parent = db.session.get(SummaryRun, root.parent_run_id)
        if parent is None:
            break
        root = parent

    chain = [root]
    frontier = [root]
    while frontier:
        nxt = []
        for r in frontier:
            children = (
                SummaryRun.query.filter_by(parent_run_id=r.id)
                .order_by(SummaryRun.revision.asc(), SummaryRun.generated_at.asc())
                .all()
            )
            nxt.extend(children)
        chain.extend(nxt)
        frontier = nxt

    chain.sort(key=lambda r: (r.revision or 1, r.generated_at or utcnow()))
    return chain


def edition_heads(summary: Summary):
    """Return the latest revision of each edition chain for a summary, newest first."""
    child_ids = [
        r.parent_run_id
        for r in SummaryRun.query.filter_by(summary_id=summary.id)
        .filter(SummaryRun.parent_run_id.isnot(None))
        .all()
    ]
    q = SummaryRun.query.filter_by(summary_id=summary.id)
    if child_ids:
        q = q.filter(~SummaryRun.id.in_(child_ids))
    return q.order_by(SummaryRun.generated_at.desc()).all()


# ─────────────────────────── scheduled edition cutting ────────────────────────────

def cut_due_editions(force: bool = False) -> int:
    """Cut new editions for all fixed_period summaries whose release time has arrived.

    When ``force`` is True the time-has-passed guard is skipped, which is useful
    for debug-mode startup so editions are generated immediately.

    Returns the number of editions cut.
    """
    cut = 0
    # Templates rendered by build_summary() (e.g. _news_item.html) call url_for(),
    # which requires a request context. The scheduler and startup-seed callers of
    # this function only push an app context, so provide one here.
    with current_app.test_request_context(base_url=current_app.config.get("PUBLIC_URL", "")):
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
                # Only cut if the cutoff time has actually passed (skipped when force=True)
                if not force and now.replace(tzinfo=None) < expected_naive:
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

    with current_app.test_request_context(base_url=cfg.get("PUBLIC_URL", "")):
        open_url = url_for(
            "web.edition_view", summary_id=summary.id, run_id=run.id, _external=True
        )
        app_css_url = url_for("static", filename="css/app.css", _external=True)

    full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{subject}</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="{app_css_url}">
<style>
  body {{ max-width: 700px; margin: 0 auto; background: #f8f9fa; }}
  .email-header {{
    background: linear-gradient(90deg, #16293f, #1f3a5f);
    color: #fff; padding: 14px 20px; font-weight: 600; font-size: 1.1rem;
  }}
  .email-body {{ padding: 20px; background: #fff; }}
</style>
</head>
<body>
<div class="email-header">📰 AI News</div>
<div class="email-body">
<p style="color:#6c757d;font-size:.875rem;margin-bottom:1rem">{summary.name} · {run.label or ''}</p>
<p style="margin-bottom:1.5rem"><a href="{open_url}">Open this edition in the app →</a></p>
{html_body}
</div>
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
