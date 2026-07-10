"""Main web UI: dashboard, news, tags, tag try-out, summaries."""
from __future__ import annotations

import json
import queue
import secrets
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

from functools import wraps

from ..agent.runner import AgentCancelled
from ..extensions import db
from ..models import (
    ApiKey, Alert, BalanceTransaction, EditionRecipient, LemonsqueezyProduct, NewsItem,
    Source, Summary, SummaryRun, Tag, User, UserDisabledSource, utcnow,
)
from ..services import edition_mail, generation_registry, ingest, summarize
from ..sources import registry as source_registry
from ..summaries import registry as summary_registry

bp = Blueprint("web", __name__)


def approved_required(view):
    """Gate self-service source/API-key management to approved users.

    Admins are implicitly approved (see User.is_approved).
    """
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_approved:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


@bp.route("/podcast-audio/<filename>")
@login_required
def serve_podcast(filename: str):
    """Serve podcast MP3 files from the instance folder (writable across releases)."""
    import os
    from flask import send_from_directory
    podcast_dir = os.path.join(current_app.instance_path, "podcasts")
    return send_from_directory(podcast_dir, filename, mimetype="audio/mpeg")


@bp.route("/podcast/feed/<token>")
def podcast_feed(token: str):
    """Public RSS feed of a user's generated podcasts, keyed by a secret token.

    Podcast apps can't authenticate with a session cookie, so access is gated
    on the unguessable per-user ``podcast_feed_token`` embedded in the URL.
    """
    from ..services import podcast_feed as feed_svc

    user = User.query.filter_by(podcast_feed_token=token).first() or abort(404)
    episodes = feed_svc.build_episodes(user)
    for ep in episodes:
        ep["audio_url"] = url_for(
            "web.podcast_feed_audio", token=token, filename=ep["filename"],
            _external=True,
        )
    xml = render_template(
        "podcast_feed.xml",
        feed_title=f"Dispatch · {user.username}",
        feed_description="Auto-generated audio editions of your AI news digest.",
        feed_link=url_for("web.dashboard", _external=True),
        feed_author=user.username,
        feed_image_url=url_for("static", filename="icons/icon-512.png", _external=True),
        episodes=episodes,
    )
    return Response(xml, mimetype="application/rss+xml")


@bp.route("/podcast/feed/<token>/audio/<filename>")
def podcast_feed_audio(token: str, filename: str):
    """Serve a podcast MP3 to a podcast app, gated by the feed token.

    Validates that the file actually belongs to the token's owner so one user's
    token can't be used to fetch another user's audio from the shared folder.
    """
    import os
    from flask import send_from_directory
    from ..services import podcast_feed as feed_svc

    user = User.query.filter_by(podcast_feed_token=token).first() or abort(404)
    if not feed_svc.owns_audio(user, filename):
        abort(404)
    podcast_dir = os.path.join(current_app.instance_path, "podcasts")
    return send_from_directory(podcast_dir, filename, mimetype="audio/mpeg")


@bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))

    admin_emails = current_app.config.get("ADMIN_EMAILS", [])
    demo_run = None
    if admin_emails:
        demo_run = (
            SummaryRun.query
            .join(Summary, SummaryRun.summary_id == Summary.id)
            .join(User, Summary.user_id == User.id)
            .filter(SummaryRun.share_token.isnot(None), User.email.in_(admin_emails))
            .order_by(SummaryRun.generated_at.desc())
            .first()
        )

    enabled_sources = [
        s for s in Source.query.filter_by(enabled=True).order_by(Source.name).all()
        if not (s.parent_source_id is None and s.type_key == "imap_newsletter")
    ]
    source_badges = sorted({_source_badge_label(s) for s in enabled_sources})

    return render_template("index.html", demo_run=demo_run, source_badges=source_badges)


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

    enabled_sources = [
        s for s in Source.query.filter_by(enabled=True).order_by(Source.name).all()
        # A top-level imap_newsletter source is the mailbox connection itself,
        # not a source a user thinks about — its newsletter children are.
        if not (s.parent_source_id is None and s.type_key == "imap_newsletter")
    ]
    source_badges = sorted({_source_badge_label(s) for s in enabled_sources})
    has_api_key = ApiKey.query.filter_by(owner_user_id=current_user.id).first() is not None

    # Flip the flag as soon as we decide to show it — not on explicit
    # dismissal — so it reliably only ever appears once, even if the user
    # closes the tab without clicking anything.
    show_onboarding = not current_user.has_seen_onboarding
    if show_onboarding:
        current_user.has_seen_onboarding = True
        db.session.commit()

    return render_template(
        "dashboard.html",
        my_summaries=my_summaries,
        latest_editions=latest_editions,
        featured_summary=featured_summary,
        featured_run=featured_run,
        other_summaries=other_summaries,
        source_badges=source_badges,
        has_api_key=has_api_key,
        show_onboarding=show_onboarding,
    )


def _source_badge_label(source: Source) -> str:
    """Domain for a newsletter subscription, name for everything else."""
    if source.is_newsletter_subscription:
        addr = (source.config or {}).get("newsletter_sender") or ""
        if "@" in addr:
            return addr.rsplit("@", 1)[-1]
        return (source.config or {}).get("newsletter_domain") or source.name
    return source.name


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


# ───────────────────────── Search ─────────────────────────
@bp.route("/search")
@login_required
def search():
    from ..services.search import PER_PAGE, search_editions, search_news

    query = request.args.get("q", "").strip()
    ep = request.args.get("ep", 1, type=int)
    np_ = request.args.get("np", 1, type=int)

    edition_results, edition_total = [], 0
    news_results, news_total = [], 0
    if query:
        edition_results, edition_total = search_editions(query, current_user.id, ep)
        news_results, news_total = search_news(query, np_)

    return render_template(
        "search.html",
        query=query,
        edition_results=edition_results,
        edition_total=edition_total,
        ep=ep,
        news_results=news_results,
        news_total=news_total,
        np=np_,
        per_page=PER_PAGE,
    )


# ───────────────────────── Edition recipients ─────────────────────────
# The list starts seeded with just the account's own email — done once, at
# registration time (app/auth/routes.py) and via a one-off migration backfill
# for pre-existing accounts — never re-seeded lazily here, since a user who
# deliberately empties the list (e.g. to stop all edition mail) shouldn't
# have it silently repopulated the next time they open Settings.
def _sync_send_email_toggle(user: User) -> None:
    """Auto-check/uncheck "Send as email newsletter" based on whether the
    user has any confirmed recipients left."""
    summary = (
        Summary.query.filter_by(user_id=user.id, type_key="agentic_page").first()
    )
    if summary is None:
        return
    has_confirmed = user.edition_recipients.filter(
        EditionRecipient.confirmed_at.isnot(None)
    ).count() > 0
    params = dict(summary.params or {})
    if params.get("send_email") != has_confirmed:
        params["send_email"] = has_confirmed
        summary.params = params
        db.session.commit()


@bp.route("/settings/recipients", methods=["POST"])
@login_required
def recipient_add():
    email = (request.form.get("email") or "").strip().lower()
    if not email or "@" not in email:
        flash("Enter a valid email address.", "danger")
        return redirect(url_for("web.settings") + "#sec-recipients")
    if current_user.edition_recipients.filter_by(email=email).first():
        flash("That address is already on the list.", "danger")
        return redirect(url_for("web.settings") + "#sec-recipients")

    token = secrets.token_urlsafe(32)
    recipient = EditionRecipient(user_id=current_user.id, email=email, confirm_token=token)
    db.session.add(recipient)
    db.session.commit()

    confirm_url = url_for("web.recipient_confirm", token=token, _external=True)
    edition_mail.send_via_newsletter_mailbox(
        email,
        "Confirm this email for your Dispatch editions",
        f"You've been added as a recipient of {current_user.username}'s Dispatch edition "
        f"emails. Click to confirm:\n\n{confirm_url}\n\n"
        "If you didn't expect this, you can ignore this message.",
    )
    flash(f"Confirmation email sent to {email}.", "success")
    return redirect(url_for("web.settings") + "#sec-recipients")


@bp.route("/recipients/confirm/<token>")
def recipient_confirm(token: str):
    recipient = EditionRecipient.query.filter_by(confirm_token=token).first()
    if recipient is None:
        flash("That confirmation link is invalid or has already been used.", "danger")
        return redirect(url_for("auth.login"))
    recipient.confirmed_at = utcnow()
    recipient.confirm_token = None
    db.session.commit()
    _sync_send_email_toggle(recipient.user)
    flash(f"{recipient.email} will now receive Dispatch edition emails.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/settings/recipients/<int:recipient_id>/remove", methods=["POST"])
@login_required
def recipient_remove(recipient_id: int):
    recipient = db.session.get(EditionRecipient, recipient_id) or abort(404)
    if recipient.user_id != current_user.id:
        abort(403)
    email = recipient.email
    was_confirmed = recipient.is_confirmed
    db.session.delete(recipient)
    db.session.commit()
    _sync_send_email_toggle(current_user)
    if was_confirmed:
        edition_mail.send_via_newsletter_mailbox(
            email,
            "Removed from Dispatch edition emails",
            f"{email} has been removed as a recipient of {current_user.username}'s "
            "Dispatch edition emails. You will no longer receive them.",
        )
    flash(f"Removed {email}.", "info")
    return redirect(url_for("web.settings") + "#sec-recipients")


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
        if current_user.has_podcast_access:
            current_user.podcast_auto_generate = bool(request.form.get("podcast_auto_generate"))
        try:
            current_user.pdf_font_scale = max(50, min(150, int(request.form.get("pdf_font_scale") or 80)))
        except (ValueError, TypeError):
            pass
        db.session.commit()

        # Podcast format memory (user-level, no summary)
        if current_user.has_podcast_access and "mem_news_podcast_format" in request.form:
            from ..services.podcast import _set_news_podcast_format
            _set_news_podcast_format(current_user, request.form.get("mem_news_podcast_format", ""))

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

    from ..services.podcast import _get_news_podcast_format, DEFAULT_NEWS_PODCAST_FORMAT
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

    news_podcast_format = None
    podcast_feed_url = None
    if current_user.has_podcast_access:
        news_podcast_format = _get_news_podcast_format(current_user)
        feed_token = current_user.get_or_create_feed_token()
        podcast_feed_url = url_for("web.podcast_feed", token=feed_token, _external=True)

    recipients = current_user.edition_recipients.order_by(EditionRecipient.created_at).all()

    return render_template(
        "settings.html",
        summary=summary, types=types,
        files=files, headlines=headlines, retention=retention,
        news_podcast_format=news_podcast_format,
        default_news_podcast_format=DEFAULT_NEWS_PODCAST_FORMAT,
        podcast_feed_url=podcast_feed_url,
        recipients=recipients,
    )


@bp.route("/settings/podcast-feed/regenerate", methods=["POST"])
@login_required
def regenerate_podcast_feed_token():
    """Rotate the podcast-feed token, invalidating the old feed URL."""
    current_user.reset_feed_token()
    flash("Podcast feed URL regenerated. Update the subscription in your podcast app.", "success")
    return redirect(url_for("web.settings"))


# ───────────────────────── API keys ─────────────────────────
@bp.route("/keys")
@approved_required
def api_keys():
    keys = ApiKey.manageable_by(current_user)
    topup_products = LemonsqueezyProduct.query.filter_by(active=True).order_by(
        LemonsqueezyProduct.credited_amount_cents
    ).all()
    transactions = (
        current_user.balance_transactions.order_by(BalanceTransaction.created_at.desc()).limit(50).all()
    )
    return render_template(
        "keys.html", keys=keys, topup_products=topup_products, transactions=transactions,
    )


@bp.route("/keys/new", methods=["POST"])
@approved_required
def api_key_new():
    label = (request.form.get("label") or "").strip()
    secret = (request.form.get("secret") or "").strip()
    model = (request.form.get("model") or "").strip() or None
    if not label or not secret:
        flash("A label and an API key are both required.", "danger")
        return redirect(url_for("web.api_keys"))
    key = ApiKey(owner_user_id=current_user.id, label=label, provider="openrouter", model=model)
    key.set_key(secret)
    db.session.add(key)
    db.session.commit()
    flash(f'API key "{label}" added.', "success")
    return redirect(url_for("web.api_keys"))


@bp.route("/keys/<int:key_id>/revoke", methods=["POST"])
@approved_required
def api_key_revoke(key_id: int):
    key = db.session.get(ApiKey, key_id) or abort(404)
    if not key.can_manage(current_user):
        abort(403)
    key.revoked_at = utcnow()
    affected = key.sources.filter_by(enabled=True).all()
    for source in affected:
        source.enabled = False
    db.session.commit()
    msg = f'API key "{key.label}" revoked.'
    if affected:
        names = ", ".join(s.name for s in affected)
        msg += f" Disabled {len(affected)} source(s) that used it: {names}."
    flash(msg, "warning" if affected else "info")
    return redirect(url_for("web.api_keys"))


@bp.route("/keys/<int:key_id>/reactivate", methods=["POST"])
@approved_required
def api_key_reactivate(key_id: int):
    key = db.session.get(ApiKey, key_id) or abort(404)
    if not key.can_manage(current_user):
        abort(403)
    key.revoked_at = None
    db.session.commit()
    flash(f'API key "{key.label}" reactivated. Re-enable any sources that need it.', "success")
    return redirect(url_for("web.api_keys"))


@bp.route("/keys/<int:key_id>/delete", methods=["POST"])
@approved_required
def api_key_delete(key_id: int):
    key = db.session.get(ApiKey, key_id) or abort(404)
    if not key.can_manage(current_user):
        abort(403)
    if key.is_global:
        flash("The global key can't be deleted.", "danger")
        return redirect(url_for("web.api_keys"))
    if key.sources.count():
        flash("Revoke or reassign this key's sources before deleting it.", "danger")
        return redirect(url_for("web.api_keys"))
    if current_user.edition_api_key_id == key.id:
        current_user.edition_api_key_id = None
    db.session.delete(key)
    db.session.commit()
    flash("API key deleted.", "info")
    return redirect(url_for("web.api_keys"))


@bp.route("/keys/<int:key_id>/use-for-editions", methods=["POST"])
@approved_required
def api_key_use_for_editions(key_id: int):
    """Select which of the user's own keys pays for agentic edition
    generation. The shared/global key is deliberately not selectable here —
    editions are billed to the user, never silently to the shared account."""
    key = db.session.get(ApiKey, key_id) or abort(404)
    if key.is_global or key.owner_user_id != current_user.id:
        flash("Only your own keys can be used for editions.", "danger")
        return redirect(url_for("web.api_keys"))
    current_user.edition_api_key_id = key.id
    db.session.commit()
    flash(f'"{key.label}" will now be used for creating editions.', "success")
    return redirect(url_for("web.api_keys"))


# ───────────────────────── Sources ─────────────────────────
def _mailbox_address(mailbox: Source) -> str:
    cfg = mailbox.config or {}
    return cfg.get("username") or current_app.config.get("IMAP_USERNAME") or "the configured mailbox"


@bp.route("/sources")
@login_required
def sources():
    top_level = (
        Source.query.filter_by(parent_source_id=None)
        .order_by(Source.created_at.desc())
        .all()
    )
    mailbox_addresses = {
        s.id: _mailbox_address(s) for s in top_level if s.type_key == "imap_newsletter"
    }
    subscribe_id = request.args.get("subscribe", type=int)
    from datetime import timedelta
    disabled_for_me = {
        row.source_id for row in
        UserDisabledSource.query.filter_by(user_id=current_user.id).all()
    }
    return render_template(
        "sources.html", sources=top_level, mailbox_addresses=mailbox_addresses,
        subscribe_id=subscribe_id, one_week_ago=utcnow() - timedelta(days=7),
        disabled_for_me=disabled_for_me,
    )


@bp.route("/sources/<int:source_id>/toggle-mine", methods=["POST"])
@login_required
def source_toggle_mine(source_id: int):
    """Turn a (shared) source on/off for just the current user's own
    editions — independent of who owns/pays for the source."""
    source = db.session.get(Source, source_id) or abort(404)
    row = UserDisabledSource.query.filter_by(
        user_id=current_user.id, source_id=source.id,
    ).first()
    if row is None:
        db.session.add(UserDisabledSource(user_id=current_user.id, source_id=source.id))
        db.session.commit()
    else:
        db.session.delete(row)
        db.session.commit()
    return redirect(url_for("web.sources"))


@bp.route("/sources/new", methods=["GET", "POST"])
@approved_required
def source_new():
    types = {
        key: cls for key, cls in source_registry.all_types().items()
        if key != "seed" or current_user.is_admin
    }
    keys = [k for k in ApiKey.manageable_by(current_user) if k.active]
    mailbox = ingest.default_newsletter_mailbox()

    if request.method == "POST":
        type_key = request.form.get("type_key")
        plugin_cls = types.get(type_key)
        key_id = request.form.get("api_key_id", type=int)
        key = db.session.get(ApiKey, key_id) if key_id else None

        if plugin_cls is None:
            flash("Unknown source type.", "danger")
        elif key is None or key not in keys:
            flash("Choose one of your API keys for this source.", "danger")
        elif type_key == "imap_newsletter":
            if mailbox is None:
                flash("No newsletter mailbox is configured yet — ask an admin to add one first.", "danger")
            else:
                name = (request.form.get("newsletter_name") or "").strip()
                domain = ingest.normalize_domain(request.form.get("newsletter_domain"))
                if not name or not domain:
                    flash("Enter both the newsletter's name and its sending domain.", "danger")
                else:
                    child = Source(
                        type_key="imap_newsletter",
                        name=name[:120],
                        owner_user_id=current_user.id,
                        api_key_id=key.id,
                        parent_source_id=mailbox.id,
                        config={"newsletter_domain": domain, "newsletter_name": name},
                        subscription_status="waiting_confirmation",
                        enabled=True,
                    )
                    db.session.add(child)
                    db.session.commit()
                    return redirect(url_for("web.sources", subscribe=child.id))
        else:
            source = Source(
                type_key=type_key,
                name=(request.form.get("name") or plugin_cls.label).strip(),
                owner_user_id=current_user.id,
                api_key_id=key.id,
                config=_collect_source_config(plugin_cls),
                poll_interval_override=_int_or_none(
                    request.form.get("poll_interval_override")
                ),
                enabled=True,
            )
            db.session.add(source)
            db.session.commit()
            flash("Source added.", "success")
            return redirect(url_for("web.sources"))
    return render_template("sources_new.html", types=types, keys=keys, mailbox=mailbox)


@bp.route("/sources/<int:source_id>/poll-confirmation", methods=["POST"])
@login_required
def source_poll_confirmation(source_id: int):
    """Poll the parent mailbox now, so a just-sent confirmation email can be
    picked up without waiting for the next scheduled poll. Rate-limited
    server-side to once every 10 seconds regardless of how many newsletters
    share the mailbox (the client also disables the button for 10s)."""
    child = db.session.get(Source, source_id) or abort(404)
    if not child.is_newsletter_subscription or not child.can_manage(current_user):
        abort(403)
    mailbox = child.parent_source
    if mailbox is None:
        abort(404)

    from datetime import timedelta
    recently_polled = (
        mailbox.last_polled_at is not None
        and (utcnow() - _aware_dt(mailbox.last_polled_at)) < timedelta(seconds=10)
    )
    if not recently_polled:
        try:
            ingest.ingest_source(mailbox)
        except Exception:  # noqa: BLE001
            pass  # status endpoint reports current state either way
        db.session.refresh(child)

    return jsonify({
        "status": child.subscription_status,
        "last_polled_at": mailbox.last_polled_at.isoformat() if mailbox.last_polled_at else None,
    })


def _aware_dt(dt):
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@bp.route("/sources/<int:source_id>/retract", methods=["POST"])
@login_required
def source_retract(source_id: int):
    """Disable a source you own (or any, if admin) — stops polling without
    deleting its history, to save on API cost."""
    source = db.session.get(Source, source_id) or abort(404)
    if not source.can_manage(current_user):
        abort(403)
    source.enabled = False
    db.session.commit()
    flash(f'Source "{source.name}" retracted (disabled).', "info")
    return redirect(url_for("web.sources"))


@bp.route("/sources/<int:source_id>/reactivate", methods=["POST"])
@login_required
def source_reactivate(source_id: int):
    source = db.session.get(Source, source_id) or abort(404)
    if not source.can_manage(current_user):
        abort(403)
    if source.api_key_id is None or not source.api_key or not source.api_key.active:
        flash("Assign an active API key to this source before re-enabling it.", "danger")
        return redirect(url_for("web.sources"))
    source.enabled = True
    db.session.commit()
    flash(f'Source "{source.name}" re-enabled.', "success")
    return redirect(url_for("web.sources"))


@bp.route("/sources/<int:source_id>/delete", methods=["POST"])
@login_required
def source_delete(source_id: int):
    source = db.session.get(Source, source_id) or abort(404)
    if not source.can_manage(current_user):
        abort(403)
    db.session.delete(source)
    db.session.commit()
    flash("Source deleted.", "info")
    return redirect(url_for("web.sources"))


def _collect_source_config(plugin_cls) -> dict:
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


# ───────────────────────── Tags ─────────────────────────
@bp.route("/tags")
@login_required
def tags():
    global_tags = Tag.query.filter_by(scope="global").order_by(Tag.name).all()
    my_tags = (
        Tag.query.filter_by(scope="user", owner_user_id=current_user.id)
        .order_by(Tag.name)
        .all()
    )
    return render_template("tags.html", global_tags=global_tags, my_tags=my_tags)


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

    coverage = None
    if is_agentic and run.document:
        from ..services.coverage import edition_coverage
        coverage = edition_coverage(run)

    return render_template(
        "summaries/view.html",
        summary=summary, run=run, is_agentic=is_agentic, revisions=chain,
        is_shared_view=False, coverage=coverage,
    )


@bp.route("/shared/<token>")
def edition_shared(token: str):
    run = SummaryRun.query.filter_by(share_token=token).first_or_404()
    summary = run.summary
    plugin = summary_registry.get(summary.type_key)
    is_agentic = bool(plugin and getattr(plugin, "is_agentic", False))
    chain = summarize.revision_chain(run) if is_agentic else [run]
    return render_template(
        "summaries/view.html",
        summary=summary, run=run, is_agentic=is_agentic, revisions=chain,
        is_shared_view=True,
    )


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>/share", methods=["POST"])
@login_required
def edition_share(summary_id: int, run_id: int):
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)
    if not run.share_token:
        run.share_token = secrets.token_hex(32)
        db.session.commit()
    return redirect(url_for("web.edition_view", summary_id=summary_id, run_id=run_id))


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>/unshare", methods=["POST"])
@login_required
def edition_unshare(summary_id: int, run_id: int):
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)
    run.share_token = None
    db.session.commit()
    return redirect(url_for("web.edition_view", summary_id=summary_id, run_id=run_id))


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>/export")
@login_required
def edition_export(summary_id: int, run_id: int):
    import mimetypes
    import os
    import re

    from weasyprint import HTML as WPHtml
    from weasyprint.urls import default_url_fetcher

    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)
    plugin = summary_registry.get(summary.type_key)
    is_agentic = bool(plugin and getattr(plugin, "is_agentic", False))

    font_scale = max(50, min(150, current_user.pdf_font_scale or 80))
    html_str = render_template(
        "summaries/print.html",
        summary=summary, run=run, is_agentic=is_agentic,
        font_scale=font_scale,
    )

    static_folder = current_app.static_folder
    static_url_path = current_app.static_url_path  # '/static'

    def _url_fetcher(url):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.path.startswith(static_url_path + "/"):
            rel = parsed.path[len(static_url_path) + 1:]
            abs_path = os.path.join(static_folder, rel)
            if os.path.exists(abs_path):
                mime = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
                with open(abs_path, "rb") as fh:
                    return {"content": fh.read(), "mime_type": mime}
        return default_url_fetcher(url)

    pdf_bytes = WPHtml(
        string=html_str,
        base_url=request.url_root,
        url_fetcher=_url_fetcher,
    ).write_pdf()

    label = run.label or run.generated_at.strftime("%Y-%m-%d")
    safe_label = re.sub(r"[^\w\s.-]", "_", label).strip("_")
    filename = f"{safe_label}.pdf"

    # Persist the PDF so it becomes a "created" channel for this edition.
    pdf_dir = os.path.join(current_app.instance_path, "pdfs")
    os.makedirs(pdf_dir, exist_ok=True)
    stored_name = f"edition_{run.id}.pdf"
    with open(os.path.join(pdf_dir, stored_name), "wb") as fh:
        fh.write(pdf_bytes)
    if run.pdf_file != stored_name:
        run.pdf_file = stored_name
        db.session.commit()

    return Response(
        pdf_bytes,
        content_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
        },
    )


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>/pdf")
@login_required
def serve_edition_pdf(summary_id: int, run_id: int):
    """Serve the persisted PDF export for an edition (the PDF channel)."""
    import os
    from flask import send_from_directory

    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id or not run.pdf_file:
        abort(404)
    pdf_dir = os.path.join(current_app.instance_path, "pdfs")
    return send_from_directory(pdf_dir, run.pdf_file, mimetype="application/pdf")


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
    return jsonify({"read_at": None})


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>/podcast")
@login_required
def edition_podcast(summary_id: int, run_id: int):
    if not current_user.has_podcast_access:
        abort(403)
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)
    if not current_app.config.get("ELEVENLABS_API_KEY"):
        flash("Podcast export isn't configured yet — ask an admin to set it up.", "warning")
        return redirect(url_for("web.dashboard"))
    from ..services import podcast_registry
    saved_script = run.news_podcast_script or ""
    saved_audio = run.news_podcast_audio or ""
    saved_audio_url = (
        url_for("web.serve_podcast", filename=saved_audio) if saved_audio else ""
    )
    active_job = podcast_registry.get(run.id)
    return render_template(
        "summaries/podcast.html",
        summary=summary, run=run,
        auto_generate_on_release=current_user.podcast_auto_generate,
        saved_script=saved_script,
        saved_audio_url=saved_audio_url,
        active_kind=active_job.kind if active_job else "",
    )


@bp.route(
    "/summaries/<int:summary_id>/editions/<int:run_id>/podcast/start",
    methods=["POST"],
)
@login_required
def edition_podcast_start(summary_id: int, run_id: int):
    """Start a background podcast job (script / audio / full / revise).

    Returns immediately; the browser watches progress via the events stream.
    If a job is already running for this run, it is left alone (idempotent
    re-attach), so a double click or a stale tab can't spawn a duplicate.
    """
    import threading

    from ..services import podcast as podcast_svc
    from ..services import podcast_registry

    if not current_user.has_podcast_access:
        abort(403)
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)

    existing = podcast_registry.get(run.id)
    if existing is not None:
        return jsonify({"ok": True, "kind": existing.kind, "already_running": True})

    data = request.get_json(silent=True) or {}
    kind = data.get("kind", "full")
    if kind not in ("script", "audio", "full", "revise"):
        return jsonify({"error": "Invalid job kind."}), 400
    feedback = (data.get("feedback") or "").strip() or None
    if kind == "revise" and not feedback:
        return jsonify({"error": "No feedback provided."}), 400

    job = podcast_registry.start(run.id, kind, feedback)
    app = current_app._get_current_object()
    threading.Thread(
        target=podcast_svc.run_podcast_job,
        args=(app, job, run.id, current_user.id),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "kind": kind})


@bp.route("/summaries/<int:summary_id>/editions/<int:run_id>/podcast/events")
@login_required
def edition_podcast_events(summary_id: int, run_id: int):
    """SSE stream of the active podcast job's events (re-attachable)."""
    from ..services import podcast_registry

    if not current_user.has_podcast_access:
        abort(403)
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)

    job = podcast_registry.get(run.id)

    def _idle():
        yield f"data: {json.dumps({'type': 'idle'})}\n\n"

    if job is None:
        return Response(
            stream_with_context(_idle()),
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _stream():
        q = job.subscribe()
        try:
            while True:
                event = q.get()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
        finally:
            job.unsubscribe(q)

    return Response(
        stream_with_context(_stream()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route(
    "/summaries/<int:summary_id>/editions/<int:run_id>/podcast/save-script",
    methods=["POST"],
)
@login_required
def edition_podcast_save_script(summary_id: int, run_id: int):
    if not current_user.has_podcast_access:
        abort(403)
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    run = db.session.get(SummaryRun, run_id) or abort(404)
    if run.summary_id != summary_id:
        abort(404)
    data = request.get_json(silent=True) or {}
    script = (data.get("script") or "").strip()
    if not script:
        return jsonify({"error": "No script provided."}), 400
    run.news_podcast_script = script
    db.session.commit()
    return jsonify({"ok": True})


@bp.route(
    "/summaries/<int:summary_id>/editions/<int:run_id>/podcast/set-auto-generate",
    methods=["POST"],
)
@login_required
def edition_podcast_set_auto(summary_id: int, run_id: int):
    """Persist the user's 'auto-generate podcast on edition release' preference."""
    if not current_user.has_podcast_access:
        abort(403)
    summary = db.session.get(Summary, summary_id) or abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    data = request.get_json(silent=True) or {}
    current_user.podcast_auto_generate = bool(data.get("enabled"))
    db.session.commit()
    return jsonify({"ok": True, "enabled": current_user.podcast_auto_generate})


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
