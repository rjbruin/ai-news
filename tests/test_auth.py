from app.auth import tokens
from app.models import User


def test_register_creates_user(client, db):
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
    assert User.query.filter_by(email="newbie@example.com").first() is not None


def test_register_rejects_duplicate_email(client, user):
    resp = client.post(
        "/auth/register",
        data={"username": "other", "email": user.email, "password": "password123",
              "confirm": "password123"},
        follow_redirects=True,
    )
    assert b"already registered" in resp.data


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
