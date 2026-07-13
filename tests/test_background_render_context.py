"""Regression test for a crash that only reproduces OUTSIDE a request
context: edition generate/revise run render_template() from a background
thread that pushes only app.app_context() (see app/web/routes.py's
generate_stream/edition_feedback_stream _run() functions), never a request
context. inject_globals() (app/__init__.py) used to access
current_user.is_authenticated unconditionally — current_user resolves to
None without a request context (flask_login.utils._get_user), so that
crashed every background render with 'NoneType' object has no attribute
'is_authenticated'.

Deliberately does NOT use the shared `app`/`db` fixtures from conftest.py:
pytest-flask's autouse _push_request_context fixture pushes a real
app.test_request_context() for any test that depends on the `app` fixture
(directly or via `db`) — see venv/.../pytest_flask/plugin.py — which
silently masks this exact bug from the rest of the test suite. This test
creates its own app and only enters app_context(), matching the real
background-thread environment.
"""
from app import create_app
from app.config import TestConfig
from app.extensions import db as _db


def test_render_html_succeeds_without_a_request_context():
    from app.agent import render

    app = create_app(TestConfig)
    with app.app_context():
        _db.create_all()
        try:
            html = render.render_html([{"type": "divider", "id": "d1"}])
        finally:
            _db.session.remove()
            _db.drop_all()
    assert "<hr" in html or "divider" in html.lower() or html
