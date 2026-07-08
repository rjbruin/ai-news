from app.models import AdminSettings, Invite, User


def test_register_closed_by_default_without_invite(client, db):
    resp = client.get("/auth/register")
    assert resp.status_code == 200
    assert b"invite-only" in resp.data
    assert b"name=\"email\"" not in resp.data  # no form rendered


def test_register_open_when_registration_open_flag_set(client, db):
    AdminSettings.get().registration_open = True
    db.session.commit()
    resp = client.get("/auth/register")
    assert resp.status_code == 200
    assert b"name=\"email\"" in resp.data


def test_register_open_with_valid_invite_link(client, db):
    invite = Invite(code="valid-code-123", max_uses=1)
    db.session.add(invite)
    db.session.commit()

    resp = client.get("/auth/register?invite=valid-code-123")
    assert resp.status_code == 200
    assert b"name=\"email\"" in resp.data
    assert b"valid-code-123" in resp.data  # carried in the hidden field


def test_register_closed_with_invalid_invite_code(client, db):
    resp = client.get("/auth/register?invite=does-not-exist")
    assert resp.status_code == 200
    assert b"invite-only" in resp.data


def test_register_closed_with_exhausted_invite(client, db):
    invite = Invite(code="used-up", max_uses=1, uses_count=1)
    db.session.add(invite)
    db.session.commit()

    resp = client.get("/auth/register?invite=used-up")
    assert b"invite-only" in resp.data


def test_register_closed_with_revoked_invite(client, db):
    from app.models import utcnow

    invite = Invite(code="revoked-code", max_uses=5, revoked_at=utcnow())
    db.session.add(invite)
    db.session.commit()

    resp = client.get("/auth/register?invite=revoked-code")
    assert b"invite-only" in resp.data


def test_register_post_with_valid_invite_creates_account_and_consumes_use(client, db):
    invite = Invite(code="one-time-code", max_uses=2)
    db.session.add(invite)
    db.session.commit()

    resp = client.post(
        "/auth/register",
        data={
            "username": "invited", "email": "invited@dispatch-users.test-domain.com",
            "password": "password123", "confirm": "password123",
            "invite_code": "one-time-code", "submit": "Create account",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert User.query.filter_by(email="invited@dispatch-users.test-domain.com").first() is not None

    db.session.refresh(invite)
    assert invite.uses_count == 1
    assert invite.is_usable  # still has one use left


def test_register_post_without_invite_rejected_when_closed(client, db):
    resp = client.post(
        "/auth/register",
        data={
            "username": "sneaky", "email": "sneaky@dispatch-users.test-domain.com",
            "password": "password123", "confirm": "password123", "submit": "Create account",
        },
        follow_redirects=True,
    )
    assert User.query.filter_by(email="sneaky@dispatch-users.test-domain.com").first() is None


def test_register_post_with_exhausted_invite_rejected(client, db):
    invite = Invite(code="already-used", max_uses=1, uses_count=1)
    db.session.add(invite)
    db.session.commit()

    resp = client.post(
        "/auth/register",
        data={
            "username": "toolate", "email": "toolate@dispatch-users.test-domain.com",
            "password": "password123", "confirm": "password123",
            "invite_code": "already-used", "submit": "Create account",
        },
        follow_redirects=True,
    )
    assert b"invalid or has already been used up" in resp.data
    assert User.query.filter_by(email="toolate@dispatch-users.test-domain.com").first() is None


# ───────────────────────── admin management ─────────────────────────
def test_admin_can_create_invite(admin_client, db):
    resp = admin_client.post("/admin/invites/new", data={"max_uses": "3"}, follow_redirects=True)
    assert resp.status_code == 200
    invite = Invite.query.first()
    assert invite is not None
    assert invite.max_uses == 3
    assert invite.uses_count == 0


def test_non_admin_cannot_create_invite(auth_client, db):
    resp = auth_client.post("/admin/invites/new", data={"max_uses": "1"})
    assert resp.status_code == 403


def test_admin_can_revoke_invite(admin_client, db):
    invite = Invite(code="revokeme", max_uses=5)
    db.session.add(invite)
    db.session.commit()

    resp = admin_client.post(f"/admin/invites/{invite.id}/revoke", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(invite)
    assert invite.revoked_at is not None
    assert not invite.is_usable


def test_admin_can_delete_invite(admin_client, db):
    invite = Invite(code="deleteme", max_uses=1)
    db.session.add(invite)
    db.session.commit()
    invite_id = invite.id

    resp = admin_client.post(f"/admin/invites/{invite_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert db.session.get(Invite, invite_id) is None


def test_admin_can_toggle_registration_open(admin_client, db):
    assert AdminSettings.get().registration_open is False
    admin_client.post("/admin/registration-open/toggle", follow_redirects=True)
    assert AdminSettings.get().registration_open is True
    admin_client.post("/admin/registration-open/toggle", follow_redirects=True)
    assert AdminSettings.get().registration_open is False


def test_admin_page_shows_invites_section(admin_client, db):
    invite = Invite(code="showme", max_uses=1)
    db.session.add(invite)
    db.session.commit()

    resp = admin_client.get("/admin/")
    assert resp.status_code == 200
    assert b"showme" in resp.data
    assert b"Invites" in resp.data
