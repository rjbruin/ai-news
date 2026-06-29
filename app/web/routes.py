"""Main web UI: dashboard, news, tags, tag try-out, summaries."""
from __future__ import annotations

import json

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import NewsItem, Summary, SummaryRun, Tag
from ..services import summarize
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
    recent = NewsItem.query.order_by(NewsItem.fetched_at.desc()).limit(10).all()
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
    return render_template(
        "dashboard.html",
        recent=recent,
        my_summaries=my_summaries,
        latest_editions=latest_editions,
        summary_types=summary_registry.all_types(),
    )


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
    return render_template(
        "summaries/list.html",
        summaries=mine,
        editions=editions,
        types=summary_registry.all_types(),
    )


@bp.route("/summaries/new", methods=["GET", "POST"])
@login_required
def summary_new():
    types = summary_registry.all_types()
    if request.method == "POST":
        type_key = request.form.get("type_key")
        if type_key not in types:
            flash("Unknown summary type.", "danger")
        else:
            plugin_cls = types[type_key]
            scope_mode = (
                "since_last"
                if type_key == "debug_window"
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
            db.session.commit()
            flash("Summary created.", "success")
            return redirect(url_for("web.summaries"))
    return render_template("summaries/edit.html", summary=None, types=types)


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
                if type_key == "debug_window"
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
    from ..agent.creds import MissingCredentials

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
    try:
        new_run = summarize.revise_edition(run, text)
    except MissingCredentials as exc:
        flash(str(exc), "warning")
        return redirect(url_for("web.settings"))
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not revise edition: {exc}", "danger")
        return redirect(url_for("web.edition_view", summary_id=summary_id, run_id=run_id))
    flash("Revision created from your feedback.", "success")
    return redirect(url_for("web.edition_view", summary_id=summary.id, run_id=new_run.id))


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
        else:
            val = request.form.get(f"param_{field}")
            if val is not None:
                params[field] = val
            elif "default" in spec:
                params[field] = spec["default"]
    return params
