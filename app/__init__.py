"""Application factory for Dispatch."""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .extensions import db, login_manager, migrate


def create_app(config_object: type | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    if config_object is None:
        config_name = os.environ.get("FLASK_CONFIG", "")
        if config_name:
            import importlib
            module_path, cls_name = config_name.rsplit(".", 1)
            config_object = getattr(importlib.import_module(module_path), cls_name)
        else:
            config_object = Config
    app.config.from_object(config_object)

    # Trust one layer of reverse-proxy headers (nginx).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Ensure instance/artifact dirs exist for SQLite + generated files.
    _ensure_dirs(app)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    # Models must be imported so SQLAlchemy + Alembic see them.
    from . import models  # noqa: F401

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(models.User, int(user_id))

    # Discover plugins (sources + summaries) at startup.
    from .sources import registry as source_registry
    from .summaries import registry as summary_registry

    source_registry.discover()
    summary_registry.discover()

    # Blueprints
    from .auth.routes import bp as auth_bp
    from .web.routes import bp as web_bp
    from .web.admin import bp as admin_bp
    from .api.routes import bp as api_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(web_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp)

    register_template_helpers(app)

    # Purge editions that have no displayable content (e.g. created before
    # the content column was added).
    if not app.config.get("TESTING"):
        with app.app_context():
            _purge_empty_editions()
            _prune_agent_headlines(app)

    if app.config.get("DEBUG_SEED") and not app.config.get("TESTING"):
        with app.app_context():
            _seed_debug_data(app)

    # Background scheduler (skipped under tests / when disabled).
    if app.config.get("WORKER_ENABLED") and not app.config.get("TESTING"):
        from .scheduler.jobs import start_scheduler

        start_scheduler(app)

    return app


def _seed_debug_data(app: Flask) -> None:
    """Populate the database with fixture items and cut any missing summary editions.

    Safe to call on every startup — item insertion is idempotent via dedup_hash,
    and edition cutting only creates editions that are absent for the current window.
    """
    import logging

    from .extensions import db
    from .models import Source
    from .services import ingest as ingest_svc
    from .services import summarize as summarize_svc

    log = logging.getLogger(__name__)

    # 1. Ensure the seed source record exists.
    source = Source.query.filter_by(type_key="seed").first()
    if source is None:
        source = Source(
            name="Debug Seed Data",
            type_key="seed",
            enabled=True,
            config={},
        )
        db.session.add(source)
        db.session.commit()
        log.info("Debug seed: created seed source")

    # 2. Ingest fixture items (skips existing via dedup_hash).
    stats = ingest_svc.ingest_source(source)
    log.info(
        "Debug seed: %d new items ingested, %d skipped (already present)",
        stats["new_items"],
        stats["skipped"],
    )

    # 3. Force-cut editions for all enabled fixed-period summaries that are missing
    #    an edition for the current window.
    try:
        n = summarize_svc.cut_due_editions(force=True)
        if n:
            log.info("Debug seed: cut %d edition(s) at startup", n)
    except Exception:
        log.exception("Debug seed: failed to cut editions at startup")


def _purge_empty_editions() -> None:
    """Delete SummaryRun rows that have neither HTML content nor a file artifact."""
    import logging
    from .extensions import db
    from .models import SummaryRun

    try:
        result = (
            SummaryRun.query
            .filter(SummaryRun.content.is_(None))
            .filter(SummaryRun.artifact_ref.is_(None))
            .delete(synchronize_session=False)
        )
        if result:
            db.session.commit()
            logging.getLogger(__name__).info(
                "Startup: purged %d content-less edition(s)", result
            )
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).exception("Startup: failed to purge empty editions")


def _prune_agent_headlines(app: Flask) -> None:
    """Prune agent HEADLINES memory older than the configured retention window."""
    import logging
    from .agent import memory as agent_memory

    try:
        days = app.config.get("AGENT_HEADLINES_RETENTION_DAYS", 7)
        pruned = agent_memory.prune_headlines(days=days)
        if pruned:
            logging.getLogger(__name__).info(
                "Startup: pruned %d old headline file(s)", pruned
            )
    except Exception:
        from .extensions import db
        db.session.rollback()
        logging.getLogger(__name__).exception("Startup: failed to prune headlines")


def _ensure_dirs(app: Flask) -> None:
    base = Path(app.root_path).parent
    (base / "instance").mkdir(exist_ok=True)
    (Path(app.root_path) / "static" / "artifacts").mkdir(parents=True, exist_ok=True)


def register_template_helpers(app: Flask) -> None:
    from .version import get_version

    @app.context_processor
    def inject_globals():
        from flask_login import current_user
        from .models import Alert

        result: dict = {"app_version": get_version(), "active_alerts": []}
        if current_user.is_authenticated:
            result["active_alerts"] = (
                Alert.query
                .filter_by(user_id=current_user.id)
                .filter(Alert.dismissed_at.is_(None))
                .order_by(Alert.created_at.desc())
                .all()
            )
        return result

    @app.template_filter("url_domain")
    def url_domain_filter(url):
        """Extract bare domain from a URL for use as citation text."""
        from .agent.blocks import url_domain
        return url_domain(url)

    @app.template_filter("monthday")
    def monthday_filter(dt):
        if dt is None:
            return ""
        return dt.strftime("%-d %B") if hasattr(dt, "strftime") else str(dt)

    @app.template_filter("natural_dt")
    def natural_dt_filter(dt):
        """e.g. 'July 6th, 05:02' — for a friendlier timestamp than raw ISO."""
        if dt is None or not hasattr(dt, "strftime"):
            return str(dt) if dt else ""
        day = dt.day
        suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        return f"{dt.strftime('%B')} {day}{suffix}, {dt.strftime('%H:%M')}"

    def _render_markdown(text, *, inline: bool = False):
        import bleach
        import markdown as md
        from markupsafe import Markup

        if not text:
            return ""
        raw = md.markdown(str(text), extensions=["extra", "sane_lists"])
        allowed_tags = set(bleach.sanitizer.ALLOWED_TAGS) | {
            "p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "span", "br", "hr",
            "table", "thead", "tbody", "tr", "th", "td", "img",
        }
        allowed_attrs = {
            "a": ["href", "title", "target", "rel"],
            "img": ["src", "alt", "title"],
        }
        clean = bleach.clean(raw, tags=allowed_tags, attributes=allowed_attrs, strip=True)
        if inline and clean.startswith("<p>") and clean.endswith("</p>"):
            # A single line of Markdown always gets wrapped in one <p>...</p> —
            # strip it for callers rendering inside an inline element (<li>,
            # <a>) where a nested block-level <p> would be invalid HTML.
            clean = clean[3:-4]
        return Markup(clean)

    @app.template_filter("md")
    def markdown_filter(text):
        """Render Markdown to sanitized HTML (for agent-authored block content)."""
        return _render_markdown(text)

    @app.template_filter("mdinline")
    def markdown_inline_filter(text):
        """Like ``md``, but strips the single wrapping <p> so the result is
        safe to nest inside an inline element (<li>, <a>)."""
        return _render_markdown(text, inline=True)
