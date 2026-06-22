"""Main web UI: dashboard, news, tags, tag try-out, summaries."""
from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from ..extensions import db
from ..models import NewsItem, Summary, Tag
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
    counts = {
        "news": NewsItem.query.count(),
        "tags": Tag.query.count(),
        "summaries": Summary.query.filter_by(user_id=current_user.id).count(),
    }
    return render_template("dashboard.html", recent=recent, counts=counts)


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
    matches = None
    form_data = {"name": "", "keywords": "", "explanation": ""}
    if request.method == "POST":
        form_data = {
            "name": (request.form.get("name") or "").strip(),
            "keywords": request.form.get("keywords") or "",
            "explanation": (request.form.get("explanation") or "").strip(),
        }
        matches = tagging_engine.preview(
            form_data["name"],
            _parse_keywords(form_data["keywords"]),
            form_data["explanation"],
        )
    return render_template("tags/tryout.html", matches=matches, form_data=form_data)


# ───────────────────────── Summaries ─────────────────────────
@bp.route("/summaries")
@login_required
def summaries():
    mine = (
        Summary.query.filter_by(user_id=current_user.id)
        .order_by(Summary.created_at.desc())
        .all()
    )
    return render_template(
        "summaries/list.html", summaries=mine, types=summary_registry.all_types()
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
            summary = Summary(
                user_id=current_user.id,
                name=(request.form.get("name") or "My summary").strip(),
                type_key=type_key,
                scope_mode=request.form.get("scope_mode", "fixed_period"),
                period=request.form.get("period", "day"),
                params=_collect_params(types[type_key]),
            )
            db.session.add(summary)
            db.session.commit()
            flash("Summary created.", "success")
            return redirect(url_for("web.summaries"))
    return render_template("summaries/edit.html", summary=None, types=types)


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


@bp.route("/summaries/<int:summary_id>/view")
@login_required
def summary_view(summary_id: int):
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    artifact, items = summarize.build_summary(summary, mark_consumed=True)
    return render_template(
        "summaries/view.html", summary=summary, artifact=artifact, items=items
    )


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
        else:
            val = request.form.get(f"param_{field}")
            if val is not None:
                params[field] = val
    return params
