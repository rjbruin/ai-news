from app.models import AdminSettings, Summary, User
from app.services import edition_mail


def _dispatch(db, owner, published=True, name="Daily"):
    s = Summary(
        user_id=owner.id, name=name, type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
        is_published=published, published_name=(f"{name} Pub" if published else None),
    )
    db.session.add(s)
    db.session.commit()
    return s


def _confirm_email(db, user, email="alice@example.com"):
    user.newsletter_email = email
    user.newsletter_email_confirmed_at = user.created_at
    db.session.commit()


# ── Registration seeds the single address, confirmed ────────────────────────

def test_registration_seeds_newsletter_email(client, db):
    AdminSettings.get().registration_open = True
    db.session.commit()
    resp = client.post(
        "/auth/register",
        data={
            "username": "newperson", "email": "newperson@dispatch-users.test-domain.com",
            "password": "password123", "confirm": "password123", "submit": "Create account",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    new_user = User.query.filter_by(username="newperson").first()
    assert new_user.newsletter_email == "newperson@dispatch-users.test-domain.com"
    assert new_user.newsletter_email_is_confirmed


# ── Set / confirm / remove ───────────────────────────────────────────────────

def test_set_newsletter_email_sends_confirmation(auth_client, db, user, app, monkeypatch):
    app.config["IMAP_SMTP_HOST"] = "smtp.example.com"
    app.config["IMAP_USERNAME"] = "news@example.com"
    app.config["IMAP_PASSWORD"] = "secret"

    sent = []
    monkeypatch.setattr(
        edition_mail, "send_via_newsletter_mailbox",
        lambda to, subject, body: sent.append((to, subject, body)) or True,
    )

    resp = auth_client.post(
        "/settings/newsletter-email", data={"newsletter_email": "alice@example.com"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.newsletter_email == "alice@example.com"
    assert not user.newsletter_email_is_confirmed
    assert user.newsletter_email_confirm_token

    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == "alice@example.com"
    assert user.newsletter_email_confirm_token in body


def test_set_newsletter_email_rejects_invalid(auth_client, db, user):
    resp = auth_client.post(
        "/settings/newsletter-email", data={"newsletter_email": "not-an-email"},
        follow_redirects=True,
    )
    assert b"valid email" in resp.data
    db.session.refresh(user)
    assert user.newsletter_email is None


def test_confirm_newsletter_email(auth_client, db, user):
    user.newsletter_email = "alice@example.com"
    user.newsletter_email_confirm_token = "tok123"
    db.session.commit()

    resp = auth_client.get("/newsletter-email/confirm/tok123", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.newsletter_email_is_confirmed
    assert user.newsletter_email_confirm_token is None


def test_confirm_newsletter_email_invalid_token(auth_client):
    resp = auth_client.get("/newsletter-email/confirm/does-not-exist", follow_redirects=True)
    assert b"invalid or has already been used" in resp.data


def test_changing_address_requires_reconfirmation(auth_client, db, user):
    _confirm_email(db, user, "old@example.com")
    auth_client.post("/settings/newsletter-email", data={"newsletter_email": "new@example.com"})
    db.session.refresh(user)
    assert user.newsletter_email == "new@example.com"
    assert not user.newsletter_email_is_confirmed


def test_remove_newsletter_email_clears_subscriptions(auth_client, db, user, admin):
    _confirm_email(db, user)
    dispatch = _dispatch(db, admin)
    user.follow(dispatch)
    user.subscribe_email(dispatch)
    db.session.commit()
    assert user.is_email_subscribed(dispatch)

    resp = auth_client.post("/settings/newsletter-email/remove", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.newsletter_email is None
    assert not user.is_email_subscribed(dispatch)


# ── Per-dispatch email subscribe/unsubscribe ─────────────────────────────────

def test_email_subscribe_requires_following(auth_client, db, user, admin):
    _confirm_email(db, user)
    dispatch = _dispatch(db, admin)
    resp = auth_client.post("/dispatch/email-subscribe", data={"summary_id": dispatch.id})
    assert resp.status_code == 404
    db.session.refresh(user)
    assert not user.is_email_subscribed(dispatch)


def test_email_subscribe_requires_confirmed_email(auth_client, db, user, admin):
    dispatch = _dispatch(db, admin)
    user.follow(dispatch)
    db.session.commit()

    resp = auth_client.post(
        "/dispatch/email-subscribe", data={"summary_id": dispatch.id}, follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Confirm your newsletter email" in resp.data
    db.session.refresh(user)
    assert not user.is_email_subscribed(dispatch)


def test_email_subscribe_and_unsubscribe(auth_client, db, user, admin):
    _confirm_email(db, user)
    dispatch = _dispatch(db, admin)
    user.follow(dispatch)
    db.session.commit()

    auth_client.post("/dispatch/email-subscribe", data={"summary_id": dispatch.id})
    db.session.refresh(user)
    assert user.is_email_subscribed(dispatch)

    auth_client.post("/dispatch/email-unsubscribe", data={"summary_id": dispatch.id})
    db.session.refresh(user)
    assert not user.is_email_subscribed(dispatch)


def test_unfollow_clears_email_subscription(auth_client, db, user, admin):
    _confirm_email(db, user)
    dispatch = _dispatch(db, admin)
    user.follow(dispatch)
    user.subscribe_email(dispatch)
    db.session.commit()

    auth_client.post("/dispatch/unfollow", data={"summary_id": dispatch.id})
    db.session.refresh(user)
    assert not user.is_following(dispatch)
    assert not user.is_email_subscribed(dispatch)


def test_unpublish_clears_email_subscribers(auth_client, db, user, admin):
    _confirm_email(db, admin, "admin@example.com")
    own = _dispatch(db, admin, published=True, name="Admin's")
    other = User(username="reader", email="reader@example.com", email_verified=True)
    other.set_password("password123")
    db.session.add(other)
    db.session.commit()
    _confirm_email(db, other, "reader@example.com")
    other.follow(own)
    other.subscribe_email(own)
    db.session.commit()
    assert other.is_email_subscribed(own)

    # Log in as the dispatch owner to unpublish it.
    auth_client.get("/auth/logout")
    auth_client.post(
        "/auth/login", data={"email": admin.email, "password": "password123", "submit": "Sign in"},
    )
    auth_client.post("/dispatch/publish", data={"is_published": ""})
    db.session.refresh(own)
    assert own.is_published is False
    db.session.refresh(other)
    assert not other.is_email_subscribed(own)


# ── Email sending on cut editions ────────────────────────────────────────────

def test_send_edition_email_mails_confirmed_subscribers_only(app, db, user, admin, monkeypatch):
    from app.models import SummaryRun
    from app.services.summarize import _send_edition_email

    app.config["SMTP_HOST"] = "smtp.example.com"
    dispatch = _dispatch(db, admin)
    _confirm_email(db, user, "subscriber@example.com")
    user.follow(dispatch)
    user.subscribe_email(dispatch)
    db.session.commit()

    run = SummaryRun(summary_id=dispatch.id, label="Monday", status="ok")
    db.session.add(run)
    db.session.commit()

    sent = {}

    class FakeSMTP:
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
            sent["to"] = to_addrs

    monkeypatch.setattr("app.services.summarize.smtplib.SMTP", FakeSMTP)
    with app.test_request_context():
        _send_edition_email(dispatch, run, "<p>hi</p>")

    assert sent["to"] == ["subscriber@example.com"]


def test_send_via_newsletter_mailbox_logs_when_unconfigured(app):
    with app.app_context():
        app.config["IMAP_SMTP_HOST"] = ""
        assert edition_mail.send_via_newsletter_mailbox("x@example.com", "Subject", "Body") is False
