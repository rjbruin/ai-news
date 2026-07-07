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
from ..models import (
    Alert,
    ApiKey,
    IngestRun,
    NewsItem,
    NewsItemTag,
    Source,
    Summary,
    SummaryRun,
    Tag,
    User,
)
from ..services import ingest
from ..services.summarize import resend_edition_email
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
    recent_runs = (
        SummaryRun.query
        .join(Summary, SummaryRun.summary_id == Summary.id)
        .order_by(SummaryRun.generated_at.desc())
        .limit(20)
        .all()
    )
    return render_template(
        "admin/index.html",
        sources=Source.query.filter_by(parent_source_id=None)
        .order_by(Source.created_at.desc())
        .all(),
        users=User.query.order_by(User.created_at).all(),
        source_types=source_registry.all_types(),
        recent_runs=recent_runs,
    )


@bp.route("/sources/new", methods=["GET", "POST"])
@admin_required
def source_new():
    types = source_registry.all_types()
    keys = [k for k in ApiKey.manageable_by(current_user) if k.active]
    if request.method == "POST":
        type_key = request.form.get("type_key")
        plugin_cls = types.get(type_key)
        key_id = request.form.get("api_key_id", type=int)
        key = db.session.get(ApiKey, key_id) if key_id else None
        if plugin_cls is None:
            flash("Unknown source type.", "danger")
        elif key is None or key not in keys:
            flash("Choose an API key for this source.", "danger")
        else:
            source = Source(
                type_key=type_key,
                name=(request.form.get("name") or plugin_cls.label).strip(),
                api_key_id=key.id,
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
    return render_template("admin/source_edit.html", source=None, types=types, keys=keys)


@bp.route("/sources/<int:source_id>/poll", methods=["POST"])
@admin_required
def source_poll(source_id: int):
    source = db.session.get(Source, source_id) or abort(404)
    if source.is_newsletter_subscription:
        flash(
            "This is a newsletter detected inside a mailbox source — poll the mailbox instead.",
            "danger",
        )
        return redirect(url_for("admin.index"))
    stats = ingest.ingest_source(source)
    msg = (
        f"Polled '{source.name}': {stats['fetched']} emails fetched, "
        f"{stats['new_items']} new items, {stats['tagged']} tagged, "
        f"{stats['skipped']} skipped (duplicate), {stats['errors']} errors."
    )
    if stats["error_log"]:
        msg += " Errors: " + " | ".join(stats["error_log"][:5])
    flash(msg, "success" if not stats["errors"] else "warning")
    return redirect(url_for("admin.index"))


@bp.route("/sources/<int:source_id>/reindex-newsletters", methods=["POST"])
@admin_required
def source_reindex_newsletters(source_id: int):
    """Scan the whole mailbox (sender + subject only) to detect newsletter
    subscriptions up front, rather than waiting for each one to send new mail
    after a regular poll. Only valid for a top-level newsletter mailbox."""
    source = db.session.get(Source, source_id) or abort(404)
    if source.type_key != "imap_newsletter" or source.is_newsletter_subscription:
        flash("Reindexing is only available for a top-level newsletter mailbox source.", "danger")
        return redirect(url_for("admin.index"))
    try:
        stats = ingest.reindex_newsletter_mailbox(source)
    except Exception as exc:  # noqa: BLE001
        flash(f"Reindex failed: {exc}", "danger")
        return redirect(url_for("admin.index"))
    flash(
        f"Reindexed '{source.name}': {stats['messages_scanned']} email(s) scanned, "
        f"{stats['unique_senders']} unique sender(s), {stats['newsletters_detected']} "
        f"newsletter(s) detected, {stats['new_subscriptions']} new subscription(s) added.",
        "success",
    )
    return redirect(url_for("admin.index"))


@bp.route("/sources/<int:source_id>/reset", methods=["POST"])
@admin_required
def source_reset(source_id: int):
    source = db.session.get(Source, source_id) or abort(404)
    if source.is_newsletter_subscription:
        flash(
            "This is a newsletter detected inside a mailbox source — reset the mailbox instead.",
            "danger",
        )
        return redirect(url_for("admin.source_detail", source_id=source_id))
    # Delete items first (cascades to NewsItemTag), then runs
    NewsItem.query.filter_by(source_id=source_id).delete(synchronize_session=False)
    IngestRun.query.filter_by(source_id=source_id).delete(synchronize_session=False)
    source.last_polled_at = None
    source.last_status = None
    db.session.commit()
    stats = ingest.ingest_source(source)
    msg = (
        f"Reset and re-polled '{source.name}': {stats['fetched']} emails fetched, "
        f"{stats['new_items']} new items, {stats['tagged']} tagged, "
        f"{stats['skipped']} skipped, {stats['errors']} errors."
    )
    flash(msg, "success" if not stats["errors"] else "warning")
    return redirect(url_for("admin.source_detail", source_id=source_id))


@bp.route("/sources/<int:source_id>/toggle", methods=["POST"])
@admin_required
def source_toggle(source_id: int):
    source = db.session.get(Source, source_id) or abort(404)
    source.enabled = not source.enabled
    db.session.commit()
    flash(f"Source {'enabled' if source.enabled else 'disabled'}.", "info")
    return redirect(url_for("admin.index"))


@bp.route("/sources/<int:source_id>/mark-subscribed", methods=["POST"])
@admin_required
def source_mark_subscribed(source_id: int):
    """Manual override for a newsletter subscription the automatic confirmation
    flow couldn't complete (or is still waiting on) — e.g. after the admin
    confirmed it by hand."""
    source = db.session.get(Source, source_id) or abort(404)
    if not source.is_newsletter_subscription:
        flash("Only newsletter subscriptions have a confirmation status.", "danger")
        return redirect(url_for("admin.index"))
    source.subscription_status = "subscribed"
    db.session.commit()
    if source.owner_user_id:
        Alert.push(
            source.owner_user_id,
            key=f"newsletter_subscribed_{source.id}",
            message=f'Your newsletter subscription to "{source.name}" is now active.',
            level="success",
        )
    flash(f'Marked "{source.name}" as subscribed.', "success")
    return redirect(url_for("admin.index"))


@bp.route("/sources/<int:source_id>/delete", methods=["POST"])
@admin_required
def source_delete(source_id: int):
    source = db.session.get(Source, source_id) or abort(404)
    db.session.delete(source)
    db.session.commit()
    flash("Source deleted.", "info")
    return redirect(url_for("admin.index"))


@bp.route("/runs/<int:run_id>/resend-email", methods=["POST"])
@admin_required
def run_resend_email(run_id: int):
    run = db.session.get(SummaryRun, run_id) or abort(404)
    summary = run.summary
    try:
        resend_edition_email(summary, run)
        flash(
            f"Email resent: \"{run.label or run.id}\" → {summary.user.email}",
            "success",
        )
    except Exception as exc:
        flash(f"Failed to resend email: {exc}", "danger")
    return redirect(url_for("admin.index"))


@bp.route("/poll-all", methods=["POST"])
@admin_required
def poll_all():
    totals = ingest.ingest_all_due(force=True)
    flash(
        f"Polled {totals['sources']} sources: {totals['new_items']} new items, "
        f"{totals['tagged']} tagged, {totals['errors']} errors.",
        "success" if not totals["errors"] else "warning",
    )
    return redirect(url_for("admin.index"))


@bp.route("/retag", methods=["POST"])
@admin_required
def retag():
    count = ingest.retag_all()
    flash(f"Re-tagged {count} items.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/sources/<int:source_id>")
@admin_required
def source_detail(source_id: int):
    source = db.session.get(Source, source_id) or abort(404)
    runs = (
        IngestRun.query.filter_by(source_id=source_id)
        .order_by(IngestRun.fetched_at.desc())
        .all()
    )
    # Items without an ingest_run (ingested before IngestRun was added)
    orphan_items = (
        NewsItem.query.filter_by(source_id=source_id, ingest_run_id=None)
        .order_by(NewsItem.fetched_at.desc())
        .all()
    )
    return render_template(
        "admin/source_detail.html", source=source, runs=runs, orphan_items=orphan_items
    )


@bp.route("/tagging")
@admin_required
def tagging_log():
    items = (
        NewsItem.query.order_by(NewsItem.fetched_at.desc()).limit(500).all()
    )
    return render_template("admin/tagging_log.html", items=items)


@bp.route("/users/<int:user_id>/approve", methods=["POST"])
@admin_required
def user_approve(user_id: int):
    """Toggle whether a user may add their own sources / API keys."""
    user = db.session.get(User, user_id) or abort(404)
    user.approved = not user.approved
    db.session.commit()
    flash(
        f'{user.username} is now {"approved" if user.approved else "unapproved"}.',
        "success",
    )
    return redirect(url_for("admin.index"))


@bp.route("/tags/<int:tag_id>/promote", methods=["POST"])
@admin_required
def tag_promote(tag_id: int):
    tag = db.session.get(Tag, tag_id) or abort(404)
    tag.scope = "global"
    db.session.commit()
    flash(f'Tag "{tag.name}" promoted to global.', "success")
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
