"""Admin blueprint: manage sources, trigger polls, promote tags, retag."""
from __future__ import annotations

from functools import wraps

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
from ..models import Source, Tag, User
from ..services import ingest
from ..sources import registry as source_registry

bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


@bp.route("/")
@admin_required
def index():
    return render_template(
        "admin/index.html",
        sources=Source.query.order_by(Source.created_at.desc()).all(),
        users=User.query.order_by(User.created_at).all(),
        source_types=source_registry.all_types(),
    )


@bp.route("/sources/new", methods=["GET", "POST"])
@admin_required
def source_new():
    types = source_registry.all_types()
    if request.method == "POST":
        type_key = request.form.get("type_key")
        plugin_cls = types.get(type_key)
        if plugin_cls is None:
            flash("Unknown source type.", "danger")
        else:
            source = Source(
                type_key=type_key,
                name=(request.form.get("name") or plugin_cls.label).strip(),
                config=_collect_config(plugin_cls),
                poll_interval_override=_int_or_none(
                    request.form.get("poll_interval_override")
                ),
                enabled=True,
            )
            db.session.add(source)
            db.session.commit()
            flash("Source added.", "success")
            return redirect(url_for("admin.index"))
    return render_template("admin/source_edit.html", source=None, types=types)


@bp.route("/sources/<int:source_id>/poll", methods=["POST"])
@admin_required
def source_poll(source_id: int):
    source = db.session.get(Source, source_id) or abort(404)
    stats = ingest.ingest_source(source)
    flash(
        f"Polled “{source.name}”: {stats['new_items']} new, "
        f"{stats['tagged']} tagged, {stats['errors']} errors.",
        "success" if not stats["errors"] else "warning",
    )
    return redirect(url_for("admin.index"))


@bp.route("/sources/<int:source_id>/toggle", methods=["POST"])
@admin_required
def source_toggle(source_id: int):
    source = db.session.get(Source, source_id) or abort(404)
    source.enabled = not source.enabled
    db.session.commit()
    flash(f"Source {'enabled' if source.enabled else 'disabled'}.", "info")
    return redirect(url_for("admin.index"))


@bp.route("/sources/<int:source_id>/delete", methods=["POST"])
@admin_required
def source_delete(source_id: int):
    source = db.session.get(Source, source_id) or abort(404)
    db.session.delete(source)
    db.session.commit()
    flash("Source deleted.", "info")
    return redirect(url_for("admin.index"))


@bp.route("/poll-all", methods=["POST"])
@admin_required
def poll_all():
    totals = ingest.ingest_all_due(force=True)
    flash(
        f"Polled {totals['sources']} sources: {totals['new_items']} new items.",
        "success",
    )
    return redirect(url_for("admin.index"))


@bp.route("/retag", methods=["POST"])
@admin_required
def retag():
    count = ingest.retag_all()
    flash(f"Re-tagged {count} items.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/tags/<int:tag_id>/promote", methods=["POST"])
@admin_required
def tag_promote(tag_id: int):
    tag = db.session.get(Tag, tag_id) or abort(404)
    tag.scope = "global"
    db.session.commit()
    flash(f"Tag “{tag.name}” promoted to global.", "success")
    return redirect(url_for("web.tags"))


# ───────────────────────── helpers ─────────────────────────
def _collect_config(plugin_cls) -> dict:
    config = {}
    for field, spec in (plugin_cls.config_schema or {}).items():
        if spec.get("type") == "checkbox":
            config[field] = bool(request.form.get(f"cfg_{field}"))
        else:
            val = request.form.get(f"cfg_{field}")
            if val:
                config[field] = val
    return config


def _int_or_none(val):
    try:
        return int(val) if val else None
    except (TypeError, ValueError):
        return None
