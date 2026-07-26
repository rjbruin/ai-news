from app.models import Summary, SummaryRun
from app.services.summarize import _block_css


def _dispatch(db, owner):
    s = Summary(
        user_id=owner.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(s)
    db.session.commit()
    return s


class FakeSMTP:
    sent_html = None

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, from_addr, to_addrs, msg):
        FakeSMTP.sent_html = msg


def test_block_css_non_empty_and_has_known_marker(app):
    with app.app_context():
        css = _block_css()
    assert css.strip()
    assert ".agentic-quick-hits" in css
    assert ".an-h1" in css


def test_email_includes_block_css_and_font_import(app, db, user, admin, monkeypatch):
    from app.services.summarize import _send_edition_email

    app.config["SMTP_HOST"] = "smtp.example.com"
    dispatch = _dispatch(db, admin)
    user.newsletter_email = "subscriber@example.com"
    from app.models import utcnow
    user.newsletter_email_confirmed_at = utcnow()
    user.follow(dispatch)
    user.subscribe_email(dispatch)
    db.session.commit()

    run = SummaryRun(summary_id=dispatch.id, label="Monday", status="ok")
    db.session.add(run)
    db.session.commit()

    monkeypatch.setattr("app.services.summarize.smtplib.SMTP", FakeSMTP)
    with app.test_request_context():
        _send_edition_email(dispatch, run, "<p>hi</p>")

    import email as email_module

    raw = FakeSMTP.sent_html
    assert raw is not None
    msg = email_module.message_from_string(raw)
    html_part = next(p for p in msg.walk() if p.get_content_type() == "text/html")
    html = html_part.get_payload(decode=True).decode("utf-8")
    # Google Fonts import present (the "match fonts" request).
    assert "@import url('https://fonts.googleapis.com" in html
    # Previously-missing rules from the app.css/_EMAIL_CSS gap analysis.
    assert ".agentic-callout--trend .an-label" in html
    assert ".agentic-quick-hits li::before" in html
    assert ".an-h1" in html
    # Email-only envelope still present.
    assert "email-header" in html
    assert "email-body" in html


def test_block_css_missing_file_raises(app, monkeypatch):
    from app.services import summarize

    summarize._block_css.cache_clear()
    monkeypatch.setattr(
        summarize.os.path, "join",
        lambda *a: "/nonexistent/path/blocks.css",
    )
    with app.app_context():
        try:
            summarize._block_css()
            assert False, "expected FileNotFoundError"
        except FileNotFoundError:
            pass
    summarize._block_css.cache_clear()
