from app.auth import tokens
from app.models import AdminSettings, User


def _open_registration(db):
    AdminSettings.get().registration_open = True
    db.session.commit()


def test_register_creates_user(client, db):
    _open_registration(db)
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
    _open_registration(db)
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
    _open_registration(db)
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
    _open_registration(db)
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
