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
from ..models import NewsItem, Summary, SummaryRun, Tag, utcnow
from ..services import generation_registry, summarize
from ..summaries import registry as summary_registry
from ..tagging import engine as tagging_engine

bp = Blueprint("web", __name__)


@bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))
    return render_template("index.html")


@bp.route("/dashboard")
@login_required
def dashboard():
    my_summaries = (
        Summary.query.filter_by(user_id=current_user.id)
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
        summary_types=summary_registry.all_types(),
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
    if request.method == "POST":
        current_user.openrouter_model = (
            request.form.get("openrouter_model") or ""
        ).strip() or None

        if request.form.get("clear_key"):
            current_user.set_openrouter_key(None)
            flash("OpenRouter API key removed.", "info")
        else:
            new_key = (request.form.get("openrouter_api_key") or "").strip()
            if new_key:
                current_user.set_openrouter_key(new_key)
                flash("OpenRouter API key saved.", "success")
            else:
                flash("Settings updated.", "success")

        db.session.commit()
        return redirect(url_for("web.settings"))

    return render_template("settings.html")


# ───────────────────────── News ─────────────────────────
@bp.route("/news")
@login_required
def news():
    tag_id = request.args.get("tag", type=int)
    q = NewsItem.query
    if tag_id:
        q = q.join(NewsItem.tag_links).filter_by(tag_id=tag_id)
    items = q.order_by(NewsItem.fetched_at.desc()).limit(100).all()
    tags = Tag.query.order_by(Tag.name).all()
    return render_template("news.html", items=items, tags=tags, active_tag=tag_id)


@bp.route("/news/<int:item_id>/read")
@login_required
def news_read(item_id: int):
    item = db.session.get(NewsItem, item_id) or abort(404)
    if not item.full_text:
        abort(404)
    return render_template("news_read.html", item=item)


# ───────────────────────── Tags / taxonomy ─────────────────────────
@bp.route("/tags")
@login_required
def tags():
    global_tags = Tag.query.filter_by(scope="global").order_by(Tag.name).all()
    my_tags = (
        Tag.query.filter_by(scope="user", owner_user_id=current_user.id)
        .order_by(Tag.name)
        .all()
    )
    return render_template("tags/list.html", global_tags=global_tags, my_tags=my_tags)


@bp.route("/tags/new", methods=["GET", "POST"])
@login_required
def tag_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        keywords = _parse_keywords(request.form.get("keywords"))
        explanation = (request.form.get("explanation") or "").strip()
        make_global = bool(request.form.get("make_global")) and current_user.is_admin
        if not name:
            flash("A tag needs a name.", "danger")
        elif Tag.query.filter_by(name=name).first():
            flash("A tag with that name already exists.", "danger")
        else:
            tag = Tag(
                name=name,
                keywords=keywords,
                explanation=explanation,
                scope="global" if make_global else "user",
                owner_user_id=current_user.id,
            )
            db.session.add(tag)
            db.session.commit()
            flash(f'Tag "{name}" created.', "success")
            return redirect(url_for("web.tags"))
    return render_template("tags/edit.html", tag=None)


@bp.route("/tags/<int:tag_id>/edit", methods=["GET", "POST"])
@login_required
def tag_edit(tag_id: int):
    tag = db.session.get(Tag, tag_id) or abort(404)
    if not _can_edit_tag(tag):
        abort(403)
    if request.method == "POST":
        tag.name = (request.form.get("name") or tag.name).strip()
        tag.keywords = _parse_keywords(request.form.get("keywords"))
        tag.explanation = (request.form.get("explanation") or "").strip()
        if current_user.is_admin:
            tag.scope = "global" if request.form.get("make_global") else tag.scope
        db.session.commit()
        flash("Tag updated.", "success")
        return redirect(url_for("web.tags"))
    return render_template("tags/edit.html", tag=tag)


@bp.route("/tags/<int:tag_id>/delete", methods=["POST"])
@login_required
def tag_delete(tag_id: int):
    tag = db.session.get(Tag, tag_id) or abort(404)
    if not _can_edit_tag(tag):
        abort(403)
    db.session.delete(tag)
    db.session.commit()
    flash("Tag deleted.", "info")
    return redirect(url_for("web.tags"))


@bp.route("/tags/try-out", methods=["GET", "POST"])
@login_required
def tag_tryout():
    form_data = {"name": "", "keywords": "", "explanation": ""}
    if request.method == "POST":
        form_data = {
            "name": (request.form.get("name") or "").strip(),
            "keywords": request.form.get("keywords") or "",
            "explanation": (request.form.get("explanation") or "").strip(),
        }
        name = form_data["name"]
        if not name:
            flash("A tag needs a name.", "danger")
        elif Tag.query.filter_by(name=name).first():
            flash("A tag with that name already exists.", "danger")
        else:
            make_global = bool(request.form.get("make_global")) and current_user.is_admin
            tag = Tag(
                name=name,
                keywords=_parse_keywords(form_data["keywords"]),
                explanation=form_data["explanation"],
                scope="global" if make_global else "user",
                owner_user_id=current_user.id,
            )
            db.session.add(tag)
            db.session.commit()
            flash(f'Tag "{name}" created.', "success")
            return redirect(url_for("web.tags"))
    return render_template("tags/tryout.html", form_data=form_data)


@bp.route("/tags/try-out/stream")
@login_required
def tag_tryout_stream():
    name = (request.args.get("name") or "").strip()
    keywords = _parse_keywords(request.args.get("keywords"))
    explanation = (request.args.get("explanation") or "").strip()

    def generate():
        if not name:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Name is required'})}\n\n"
            return
        for event in tagging_engine.preview_iter(name, keywords, explanation):
            yield f"data: {json.dumps(event)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ───────────────────────── Summaries ─────────────────────────
@bp.route("/summaries")
@login_required
def summaries():
    mine = (
        Summary.query.filter_by(user_id=current_user.id)
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


@bp.route("/summaries/new", methods=["GET", "POST"])
@login_required
def summary_new():
    types = summary_registry.all_types()
    has_existing = Summary.query.filter_by(user_id=current_user.id).first() is not None
    if request.method == "POST":
        type_key = request.form.get("type_key")
        if type_key not in types:
            flash("Unknown summary type.", "danger")
        else:
            plugin_cls = types[type_key]
            scope_mode = (
                "since_last"
                if type_key in {"debug_window", "debug_agentic"}
                else request.form.get("scope_mode", "fixed_period")
            )
            summary = Summary(
                user_id=current_user.id,
                name=(request.form.get("name") or "My summary").strip(),
                type_key=type_key,
                scope_mode=scope_mode,
                period=request.form.get("period", "day"),
                params=_collect_params(plugin_cls),
            )
            db.session.add(summary)
            db.session.flush()
            if not has_existing or request.form.get("feature_this"):
                current_user.featured_summary_id = summary.id
            db.session.commit()
            flash("Summary created.", "success")
            return redirect(url_for("web.summaries"))
    return render_template(
        "summaries/edit.html", summary=None, types=types, has_existing=has_existing
    )


@bp.route("/summaries/<int:summary_id>/edit", methods=["GET", "POST"])
@login_required
def summary_edit(summary_id: int):
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    types = summary_registry.all_types()
    if request.method == "POST":
        type_key = request.form.get("type_key", summary.type_key)
        if type_key not in types:
            flash("Unknown summary type.", "danger")
        else:
            summary.name = (request.form.get("name") or summary.name).strip()
            summary.type_key = type_key
            summary.scope_mode = (
                "since_last"
                if type_key in {"debug_window", "debug_agentic"}
                else request.form.get("scope_mode", summary.scope_mode)
            )
            summary.period = request.form.get("period", summary.period)
            summary.params = _collect_params(types[type_key])
            db.session.commit()
            flash("Summary updated.", "success")
            return redirect(url_for("web.summaries"))
    return render_template("summaries/edit.html", summary=summary, types=types)


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

    handle = generation_registry.get(summary_id)
    if handle is None:
        handle = generation_registry.start(summary_id, kind="generate")
        app = current_app._get_current_object()

        def _run():
            with app.app_context():
                try:
                    s = db.session.get(Summary, summary_id)
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


@bp.route("/summaries/<int:summary_id>/memory", methods=["GET", "POST"])
@login_required
def summary_memory(summary_id: int):
    """View/edit the agent's Markdown memory files for a summary."""
    from ..agent import memory as agent_memory
    from ..agent.prompt import DEFAULT_DAILY_CONTENT_CONFIG, DEFAULT_INTERESTS

    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)

    if request.method == "POST":
        for kind in ("interests", "content_config", "history"):
            if f"mem_{kind}" in request.form:
                agent_memory.write(current_user, summary, kind, request.form.get(f"mem_{kind}", ""))
        flash("Memory updated.", "success")
        return redirect(url_for("web.summary_memory", summary_id=summary.id))

    files = {
        "interests": agent_memory.ensure_default(
            current_user, summary, "interests", DEFAULT_INTERESTS),
        "content_config": agent_memory.ensure_default(
            current_user, summary, "content_config", DEFAULT_DAILY_CONTENT_CONFIG),
        "history": agent_memory.read(current_user, summary, "history"),
    }
    retention = current_app.config.get("AGENT_HEADLINES_RETENTION_DAYS", 7)
    headlines = agent_memory.recent_headlines(current_user, summary, days=retention)
    return render_template(
        "summaries/memory.html",
        summary=summary, files=files, headlines=headlines, retention=retention,
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
def _parse_keywords(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = raw.replace("\n", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


def _can_edit_tag(tag: Tag) -> bool:
    return current_user.is_admin or tag.owner_user_id == current_user.id


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
