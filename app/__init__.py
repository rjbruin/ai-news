"""Application factory for AI News."""
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

    app.register_blueprint(auth_bp)
    app.register_blueprint(web_bp)
    app.register_blueprint(admin_bp)

    register_template_helpers(app)

    # Background scheduler (skipped under tests / when disabled).
    if app.config.get("WORKER_ENABLED") and not app.config.get("TESTING"):
        from .scheduler.jobs import start_scheduler

        start_scheduler(app)

    return app


def _ensure_dirs(app: Flask) -> None:
    base = Path(app.root_path).parent
    (base / "instance").mkdir(exist_ok=True)
    (Path(app.root_path) / "static" / "artifacts").mkdir(parents=True, exist_ok=True)


def register_template_helpers(app: Flask) -> None:
    from .version import get_version

    @app.context_processor
    def inject_globals():
        return {"app_version": get_version()}

    @app.template_filter("monthday")
    def monthday_filter(dt):
        if dt is None:
            return ""
        return dt.strftime("%-d %B") if hasattr(dt, "strftime") else str(dt)
