from app.auth import tokens
from app.models import User


def test_register_creates_user(client, db):
    resp = client.post(
        "/auth/register",
        data={
            "username": "newbie",
            "email": "newbie@dispatch-users.test-domain.com",
            "password": "password123",
            "confirm": "password123",
            "submit": "Create account",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert User.query.filter_by(email="newbie@dispatch-users.test-domain.com").first() is not None


def test_register_rejects_reserved_example_domain(client, db):
    resp = client.post(
        "/auth/register",
        data={
            "username": "newbie",
            "email": "newbie@example.com",
            "password": "password123",
            "confirm": "password123",
            "submit": "Create account",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert User.query.filter_by(email="newbie@example.com").first() is None
    assert b"can&#39;t receive mail" in resp.data or b"can't receive mail" in resp.data


def test_register_rejects_duplicate_email(client, db):
    existing = User(username="dupe", email="dupe@dispatch-users.test-domain.com", email_verified=True)
    existing.set_password("password123")
    db.session.add(existing)
    db.session.commit()

    resp = client.post(
        "/auth/register",
        data={"username": "other", "email": existing.email, "password": "password123",
              "confirm": "password123"},
        follow_redirects=True,
    )
    assert b"already registered" in resp.data


def test_register_rate_limited_after_repeated_attempts(client, db):
    for _ in range(5):
        client.post(
            "/auth/register",
            data={"username": "x", "email": "not-an-email", "password": "password123",
                  "confirm": "password123"},
            follow_redirects=True,
        )
    resp = client.post(
        "/auth/register",
        data={
            "username": "final",
            "email": "final@dispatch-users.test-domain.com",
            "password": "password123",
            "confirm": "password123",
        },
        follow_redirects=True,
    )
    assert b"Too many registration attempts" in resp.data
    assert User.query.filter_by(email="final@dispatch-users.test-domain.com").first() is None


def test_login_rate_limited_after_repeated_attempts(client, user):
    for _ in range(10):
        client.post(
            "/auth/login",
            data={"email": user.email, "password": "wrong", "submit": "Sign in"},
        )
    resp = client.post(
        "/auth/login",
        data={"email": user.email, "password": "password123", "submit": "Sign in"},
        follow_redirects=True,
    )
    assert b"Too many sign-in attempts" in resp.data
    assert b"Dashboard" not in resp.data  # correct password, but still blocked


def test_magic_link_rate_limited_after_repeated_attempts(client, user):
    for _ in range(5):
        client.post("/auth/magic-link", data={"email": user.email, "submit": "Email me a sign-in link"})
    resp = client.post(
        "/auth/magic-link",
        data={"email": user.email, "submit": "Email me a sign-in link"},
        follow_redirects=True,
    )
    assert b"Too many sign-in link requests" in resp.data


def test_password_login_and_logout(client, user):
    resp = client.post(
        "/auth/login",
        data={"email": user.email, "password": "password123", "submit": "Sign in"},
        follow_redirects=True,
    )
    assert b"Dashboard" in resp.data
    resp = client.get("/auth/logout", follow_redirects=True)
    assert b"Signed out" in resp.data


def test_wrong_password_rejected(client, user):
    resp = client.post(
        "/auth/login",
        data={"email": user.email, "password": "wrong", "submit": "Sign in"},
        follow_redirects=True,
    )
    assert b"Invalid email or password" in resp.data


def test_magic_link_token_roundtrip(app, user):
    token = tokens.generate(user, purpose="login")
    verified = tokens.verify(token, purpose="login")
    assert verified.id == user.id
    # Single-use: second verify fails.
    assert tokens.verify(token, purpose="login") is None


def test_magic_link_peek_does_not_consume_token(app, user):
    token = tokens.generate(user, purpose="login")
    # A scanner/prefetcher GET-ing the link (peek) must not burn the token.
    assert tokens.peek(token, purpose="login").id == user.id
    assert tokens.peek(token, purpose="login").id == user.id
    assert tokens.peek(token, purpose="login").id == user.id
    # The real user can still consume it afterwards.
    assert tokens.verify(token, purpose="login").id == user.id
    assert tokens.verify(token, purpose="login") is None


def test_magic_link_get_shows_confirm_page_without_logging_in(client, db, user):
    token = tokens.generate(user, purpose="login")
    resp = client.get(f"/auth/magic/{token}")
    assert resp.status_code == 200
    assert b"Confirm sign-in" in resp.data
    assert user.email.encode() in resp.data

    # GET must not have consumed the token or logged the user in.
    resp2 = client.get("/dashboard")
    assert resp2.status_code == 302  # still anonymous
    assert tokens.verify(token, purpose="login").id == user.id  # token still valid


def test_magic_link_survives_a_scanner_prefetch(client, db, user):
    """The core bug: a mail scanner GETs the link before the real user
    clicks it. With the old GET-consumes design this would silently log
    the scanner in and burn the token, leaving the real user with a
    "link is invalid or has expired" error. Confirm the link still works
    after being GET-ed any number of times, and only the POST consumes it."""
    token = tokens.generate(user, purpose="login")

    for _ in range(3):  # simulate repeated scanner prefetches
        scanner = client.application.test_client()
        r = scanner.get(f"/auth/magic/{token}")
        assert r.status_code == 200

    resp = client.post(f"/auth/magic/{token}", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Dashboard" in resp.data

    # Now actually consumed — a second POST must fail.
    resp2 = client.post(f"/auth/magic/{token}", follow_redirects=True)
    assert b"invalid or has expired" in resp2.data


def test_magic_link_post_logs_in_and_verifies_email(client, db):
    u = User(username="unverified", email="unverified@dispatch-users.test-domain.com")
    u.set_password("password123")
    db.session.add(u)
    db.session.commit()
    assert u.email_verified is False

    token = tokens.generate(u, purpose="login")
    resp = client.post(f"/auth/magic/{token}", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Dashboard" in resp.data
    db.session.refresh(u)
    assert u.email_verified is True


def test_magic_link_get_already_authenticated_redirects_to_dashboard(auth_client, db, user):
    token = tokens.generate(user, purpose="login")
    resp = auth_client.get(f"/auth/magic/{token}", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Dashboard" in resp.data


def test_magic_link_wrong_purpose_rejected(app, user):
    token = tokens.generate(user, purpose="login")
    assert tokens.verify(token, purpose="verify") is None


def test_admin_role_from_env(admin, user):
    # admin@example.com is in TestConfig.ADMIN_EMAILS.
    assert admin.is_admin is True
    assert user.is_admin is False


def test_login_required_redirects(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
