"""Main web UI: dashboard, news, tags, tag try-out, summaries."""
from __future__ import annotations

import json
import queue
import threading

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from flask_login import current_user, login_required

from ..agent.runner import AgentCancelled
from ..extensions import db
from ..models import Alert, NewsItem, Summary, SummaryRun, utcnow
from ..services import generation_registry, summarize
from ..summaries import registry as summary_registry

bp = Blueprint("web", __name__)


@bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))
    return render_template("index.html")


@bp.route("/alerts/<int:alert_id>/dismiss", methods=["POST"])
@login_required
def dismiss_alert(alert_id: int):
    alert = Alert.query.filter_by(id=alert_id, user_id=current_user.id).first_or_404()
    alert.dismissed_at = utcnow()
    db.session.commit()
    return "", 204


@bp.route("/dashboard")
@login_required
def dashboard():
    my_summaries = (
        Summary.query.filter_by(user_id=current_user.id, type_key="agentic_page")
        .order_by(Summary.created_at)
        .all()
    )
    latest_editions = {
        s.id: (
            SummaryRun.query
            .filter_by(summary_id=s.id)
            .order_by(SummaryRun.generated_at.desc())
            .first()
        )
        for s in my_summaries
    }

    featured_summary = current_user.featured_summary
    if featured_summary and featured_summary.user_id != current_user.id:
        featured_summary = None
    featured_run = latest_editions.get(featured_summary.id) if featured_summary else None
    other_summaries = [
        s for s in my_summaries if not featured_summary or s.id != featured_summary.id
    ]

    return render_template(
        "dashboard.html",
        my_summaries=my_summaries,
        latest_editions=latest_editions,
        featured_summary=featured_summary,
        featured_run=featured_run,
        other_summaries=other_summaries,
    )


@bp.route("/dashboard/feature", methods=["POST"])
@login_required
def dashboard_feature():
    summary_id = request.form.get("summary_id", type=int)
    summary = db.session.get(Summary, summary_id) if summary_id else None
    if not summary or summary.user_id != current_user.id:
        abort(404)
    current_user.featured_summary_id = summary.id
    db.session.commit()
    return redirect(url_for("web.dashboard"))


# ───────────────────────── Settings ─────────────────────────
@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    from ..agent import memory as agent_memory
    from ..agent.prompt import DEFAULT_DAILY_CONTENT_CONFIG, DEFAULT_INTERESTS

    summary = (
        Summary.query
        .filter_by(user_id=current_user.id, type_key="agentic_page")
        .first()
    )
    types = summary_registry.all_types()

    if request.method == "POST":
        # Account settings
        current_user.openrouter_model = (
            request.form.get("openrouter_model") or ""
        ).strip() or None
        if request.form.get("clear_key"):
            current_user.set_openrouter_key(None)
        else:
            new_key = (request.form.get("openrouter_api_key") or "").strip()
            if new_key:
                current_user.set_openrouter_key(new_key)
        db.session.commit()

        # Summary schedule + memory
        if summary:
            summary.period = request.form.get("period", summary.period)
            summary.params = _collect_params(types[summary.type_key])
            db.session.commit()
            for kind in ("interests", "content_config", "history"):
                if f"mem_{kind}" in request.form:
                    agent_memory.write(
                        current_user, summary, kind,
                        request.form.get(f"mem_{kind}", ""),
                    )

        flash("Settings saved.", "success")
        return redirect(url_for("web.settings"))

    files, headlines = {}, []
    retention = current_app.config.get("AGENT_HEADLINES_RETENTION_DAYS", 7)
    if summary:
        files = {
            "interests": agent_memory.ensure_default(
                current_user, summary, "interests", DEFAULT_INTERESTS),
            "content_config": agent_memory.ensure_default(
                current_user, summary, "content_config", DEFAULT_DAILY_CONTENT_CONFIG),
            "history": agent_memory.read(current_user, summary, "history"),
        }
        headlines = agent_memory.recent_headlines(current_user, summary, days=retention)

    return render_template(
        "settings.html",
        summary=summary, types=types,
        files=files, headlines=headlines, retention=retention,
    )


# ───────────────────────── News ─────────────────────────
@bp.route("/news")
@login_required
def news():
    items = NewsItem.query.order_by(NewsItem.fetched_at.desc()).limit(100).all()
    return render_template("news.html", items=items)


@bp.route("/news/<int:item_id>/read")
@login_required
def news_read(item_id: int):
    item = db.session.get(NewsItem, item_id) or abort(404)
    if not item.full_text:
        abort(404)
    return render_template("news_read.html", item=item)


# ───────────────────────── Editions ─────────────────────────
@bp.route("/summaries")
@login_required
def summaries():
    mine = (
        Summary.query.filter_by(user_id=current_user.id, type_key="agentic_page")
        .order_by(Summary.created_at.desc())
        .all()
    )
    # Show the latest revision of each edition chain (heads), newest first.
    editions = {s.id: summarize.edition_heads(s)[:20] for s in mine}
    active_generations = {
        s.id: generation_registry.get(s.id) for s in mine if generation_registry.get(s.id)
    }
    return render_template(
        "summaries/list.html",
        summaries=mine,
        editions=editions,
        types=summary_registry.all_types(),
        active_generations=active_generations,
    )


@bp.route("/summaries/<int:summary_id>/edit", methods=["GET", "POST"])
@login_required
def summary_edit(summary_id: int):
    return redirect(url_for("web.settings"))


@bp.route("/summaries/<int:summary_id>/memory", methods=["GET", "POST"])
@login_required
def summary_memory(summary_id: int):
    return redirect(url_for("web.settings"))


@bp.route("/summaries/<int:summary_id>/generate/custom", methods=["POST"])
@login_required
def generate_custom(summary_id: int):
    """Store a custom date range in the session and redirect to the live log page."""
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    plugin_cls = summary_registry.get(summary.type_key)
    if not getattr(plugin_cls, "is_agentic", False):
        abort(400)
    session[f"custom_range_{summary_id}"] = {
        "range_start": request.form.get("range_start", ""),
        "range_end": request.form.get("range_end", ""),
    }
    return redirect(url_for("web.generate_debug", summary_id=summary_id))


@bp.route("/summaries/<int:summary_id>/delete", methods=["POST"])
@login_required
def summary_delete(summary_id: int):
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    db.session.delete(summary)
    db.session.commit()
    flash("Summary deleted.", "info")
    return redirect(url_for("web.summaries"))


@bp.route("/summaries/<int:summary_id>/open")
@login_required
def summary_open(summary_id: int):
    """For since_last summaries: cut a new edition now and show it."""
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    _, _items, run = summarize.build_summary(summary, record_run=True, mark_consumed=True)
    return redirect(url_for("web.edition_view", summary_id=summary.id, run_id=run.id))


@bp.route("/summaries/<int:summary_id>/generate", methods=["POST"])
@login_required
def summary_generate(summary_id: int):
    """Generate a new edition on demand (useful for agentic summaries)."""
    from ..agent.creds import MissingCredentials

    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    try:
        _, _items, run = summarize.build_summary(summary, record_run=True)
    except MissingCredentials as exc:
        flash(str(exc), "warning")
        return redirect(url_for("web.settings"))
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not generate edition: {exc}", "danger")
        return redirect(url_for("web.summaries"))
    return redirect(url_for("web.edition_view", summary_id=summary.id, run_id=run.id))


@bp.route("/summaries/<int:summary_id>/generate/debug")
@login_required
def generate_debug(summary_id: int):
    """Landing page for an agentic generation run — streams the agent log live."""
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    plugin_cls = summary_registry.get(summary.type_key)
    if not getattr(plugin_cls, "is_agentic", False):
        abort(400)
    return render_template("summaries/generate_debug.html", summary=summary)


@bp.route("/summaries/<int:summary_id>/generate/stream")
@login_required
def generate_stream(summary_id: int):
    """SSE endpoint that runs the agent and streams its log events.

    Idempotent: if a generation is already running for this summary (e.g. the
    user navigated away and came back), this re-attaches to that run's event
    stream instead of starting a second one.
    """
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    plugin_cls = summary_registry.get(summary.type_key)
    if not getattr(plugin_cls, "is_agentic", False):
        abort(400)

    # Pop any custom date range set by generate_custom before starting the thread.
    custom_range = session.pop(f"custom_range_{summary_id}", None)

    handle = generation_registry.get(summary_id)
    if handle is None:
        handle = generation_registry.start(summary_id, kind="generate")
        app = current_app._get_current_object()

        def _run():
            with app.app_context():
                try:
                    s = db.session.get(Summary, summary_id)
                    if custom_range:
                        s.params = {**(s.params or {}), **custom_range}
                    _, _items, run = summarize.build_summary(
                        s, record_run=True, log_fn=handle.emit,
                        cancel_event=handle.cancel_event,
                    )
                    handle.emit({"type": "done", "run_id": run.id})
                except AgentCancelled:
                    handle.emit({"type": "cancelled"})
                except Exception as exc:  # noqa: BLE001
                    handle.emit({"type": "error", "message": str(exc)})
                finally:
                    generation_registry.finish(handle)

        threading.Thread(target=_run, daemon=True).start()

    return _stream_handle(handle)


def _stream_handle(handle) -> Response:
    """Subscribe to a GenerationHandle and stream its events as SSE."""
    q = handle.subscribe()

    def _stream():
        try:
            while True:
                try:
                    event = q.get(timeout=30)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error", "cancelled"):
                    break
        finally:
            handle.unsubscribe(q)

    return Response(
        stream_with_context(_stream()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>")
@login_required
def edition_view(summary_id: int, run_id: int):
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)
    plugin = summary_registry.get(summary.type_key)
    is_agentic = bool(plugin and getattr(plugin, "is_agentic", False))
    chain = summarize.revision_chain(run) if is_agentic else [run]
    return render_template(
        "summaries/view.html",
        summary=summary, run=run, is_agentic=is_agentic, revisions=chain,
    )


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>/feedback", methods=["POST"])
@login_required
def edition_feedback(summary_id: int, run_id: int):
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)

    text = (request.form.get("feedback") or "").strip()
    if not text:
        flash("Enter some feedback first.", "warning")
        return redirect(url_for("web.edition_view", summary_id=summary_id, run_id=run_id))
    session[f"feedback_{run_id}"] = text
    return redirect(url_for("web.edition_feedback_debug", summary_id=summary_id, run_id=run_id))


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>/feedback/debug")
@login_required
def edition_feedback_debug(summary_id: int, run_id: int):
    """Landing page for a feedback revision run — streams the agent log live."""
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)
    # Either there's stashed feedback to kick off a new run, or a run for this
    # summary is already in flight (e.g. reached via the summaries list Logs
    # link) and we're just re-attaching to its live stream.
    has_pending_feedback = f"feedback_{run_id}" in session
    if not has_pending_feedback and generation_registry.get(summary_id) is None:
        flash("Enter some feedback first.", "warning")
        return redirect(url_for("web.edition_view", summary_id=summary_id, run_id=run_id))
    return render_template("summaries/revise_debug.html", summary=summary, run=run)


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>/feedback/stream")
@login_required
def edition_feedback_stream(summary_id: int, run_id: int):
    """SSE endpoint that applies the stashed feedback and streams the agent log.

    Idempotent the same way as generate_stream: re-attaches to an already
    running revision instead of starting a second one.
    """
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)

    handle = generation_registry.get(summary_id)
    if handle is None:
        text = session.pop(f"feedback_{run_id}", None)
        if not text:
            abort(400)
        handle = generation_registry.start(summary_id, kind="revise", parent_run_id=run_id)
        app = current_app._get_current_object()

        def _run():
            with app.app_context():
                try:
                    parent_run = db.session.get(SummaryRun, run_id)
                    new_run = summarize.revise_edition(
                        parent_run, text, log_fn=handle.emit,
                        cancel_event=handle.cancel_event,
                    )
                    handle.emit({"type": "done", "run_id": new_run.id})
                except AgentCancelled:
                    handle.emit({"type": "cancelled"})
                except Exception as exc:  # noqa: BLE001
                    handle.emit({"type": "error", "message": str(exc)})
                finally:
                    generation_registry.finish(handle)

        threading.Thread(target=_run, daemon=True).start()

    return _stream_handle(handle)


@bp.route("/summaries/<int:summary_id>/generate/cancel", methods=["POST"])
@login_required
def generate_cancel(summary_id: int):
    """Cancel an in-flight agentic generation or revision for this summary."""
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    if generation_registry.cancel(summary_id):
        flash("Cancelling…", "info")
    return redirect(url_for("web.summaries"))


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>/logs")
@login_required
def edition_logs(summary_id: int, run_id: int):
    """Static replay of an edition's recorded agent log."""
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)
    return render_template("summaries/edition_logs.html", summary=summary, run=run)


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>/read", methods=["POST"])
@login_required
def edition_mark_read(summary_id: int, run_id: int):
    """Marks an edition read (idempotent). Called automatically after the
    reader has spent a few seconds on the edition page."""
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)
    if run.read_at is None:
        run.read_at = utcnow()
        db.session.commit()
    return jsonify({"read_at": run.read_at.isoformat()})


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>/unread", methods=["POST"])
@login_required
def edition_mark_unread(summary_id: int, run_id: int):
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)
    run.read_at = None
    db.session.commit()
    return redirect(url_for("web.edition_view", summary_id=summary_id, run_id=run_id))


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>/delete", methods=["POST"])
@login_required
def edition_delete(summary_id: int, run_id: int):
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)
    db.session.delete(run)
    db.session.commit()
    flash("Edition deleted.", "info")
    return redirect(url_for("web.summaries"))


# ───────────────────────── helpers ─────────────────────────
def _collect_params(plugin_cls) -> dict:
    params = {}
    for field, spec in (plugin_cls.param_schema or {}).items():
        if spec.get("type") == "checkbox":
            params[field] = bool(request.form.get(f"param_{field}"))
        elif spec.get("type") == "checkboxes":
            vals = request.form.getlist(f"param_{field}")
            params[field] = [int(v) for v in vals if v.lstrip("-").isdigit()]
        elif spec.get("type") == "number":
            raw = request.form.get(f"param_{field}")
            try:
                val = int(raw)
            except (TypeError, ValueError):
                val = spec.get("default")
            else:
                lo, hi = spec.get("min"), spec.get("max")
                if lo is not None:
                    val = max(lo, val)
                if hi is not None:
                    val = min(hi, val)
            params[field] = val
        else:
            val = request.form.get(f"param_{field}")
            if val is not None:
                params[field] = val
            elif "default" in spec:
                params[field] = spec["default"]
    return params
