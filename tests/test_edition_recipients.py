from app.models import EditionRecipient, Summary
from app.services import edition_mail


def _agentic_summary(db, user, send_email=False):
    s = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={"send_email": send_email},
    )
    db.session.add(s)
    db.session.commit()
    return s


def test_registration_seeds_default_recipient(client, db):
    resp = client.post(
        "/auth/register",
        data={
            "username": "newperson", "email": "newperson@dispatch-users.test-domain.com",
            "password": "password123", "confirm": "password123", "submit": "Create account",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    recipients = EditionRecipient.query.join(
        EditionRecipient.user
    ).filter_by(email="newperson@dispatch-users.test-domain.com").all()
    assert len(recipients) == 1
    assert recipients[0].email == "newperson@dispatch-users.test-domain.com"
    assert recipients[0].is_confirmed


def test_removing_last_recipient_is_not_silently_reseeded(auth_client, db, user):
    # The `user` fixture creates the row directly via the ORM, bypassing
    # register()'s seeding — so seed one explicitly here to set up the case.
    recipient = EditionRecipient(user_id=user.id, email=user.email)
    db.session.add(recipient)
    db.session.commit()
    recipient.confirmed_at = recipient.created_at
    db.session.commit()

    auth_client.post(f"/settings/recipients/{recipient.id}/remove", follow_redirects=True)
    assert EditionRecipient.query.filter_by(user_id=user.id).count() == 0

    # Visiting settings again must not silently re-add it.
    auth_client.get("/settings")
    assert EditionRecipient.query.filter_by(user_id=user.id).count() == 0


def test_add_recipient_sends_confirmation_via_newsletter_mailbox(auth_client, db, user, app, monkeypatch):
    app.config["IMAP_SMTP_HOST"] = "smtp.example.com"
    app.config["IMAP_USERNAME"] = "news@example.com"
    app.config["IMAP_PASSWORD"] = "secret"

    sent = []
    monkeypatch.setattr(
        edition_mail, "send_via_newsletter_mailbox",
        lambda to, subject, body: sent.append((to, subject, body)) or True,
    )

    resp = auth_client.post(
        "/settings/recipients", data={"email": "friend@example.com"}, follow_redirects=True,
    )
    assert resp.status_code == 200

    recipient = EditionRecipient.query.filter_by(user_id=user.id, email="friend@example.com").first()
    assert recipient is not None
    assert not recipient.is_confirmed
    assert recipient.confirm_token

    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == "friend@example.com"
    assert recipient.confirm_token in body


def test_add_recipient_rejects_invalid_email(auth_client, db, user):
    resp = auth_client.post("/settings/recipients", data={"email": "not-an-email"}, follow_redirects=True)
    assert b"valid email" in resp.data
    assert EditionRecipient.query.filter_by(user_id=user.id, email="not-an-email").first() is None


def test_add_recipient_rejects_duplicate(auth_client, db, user):
    db.session.add(EditionRecipient(user_id=user.id, email="dup@example.com"))
    db.session.commit()
    resp = auth_client.post("/settings/recipients", data={"email": "dup@example.com"}, follow_redirects=True)
    assert b"already on the list" in resp.data


def test_confirm_recipient_activates_and_checks_send_email_box(auth_client, db, user):
    summary = _agentic_summary(db, user, send_email=False)
    recipient = EditionRecipient(user_id=user.id, email="friend@example.com", confirm_token="tok123")
    db.session.add(recipient)
    db.session.commit()

    resp = auth_client.get("/recipients/confirm/tok123", follow_redirects=True)
    assert resp.status_code == 200

    db.session.refresh(recipient)
    assert recipient.is_confirmed
    assert recipient.confirm_token is None

    db.session.refresh(summary)
    assert summary.params.get("send_email") is True


def test_confirm_recipient_invalid_token(auth_client):
    resp = auth_client.get("/recipients/confirm/does-not-exist", follow_redirects=True)
    assert b"invalid or has already been used" in resp.data


def test_remove_last_recipient_unchecks_send_email_box(auth_client, db, user):
    summary = _agentic_summary(db, user, send_email=True)
    recipient = EditionRecipient(user_id=user.id, email=user.email, confirmed_at=None)
    db.session.add(recipient)
    db.session.commit()
    recipient.confirmed_at = recipient.created_at
    db.session.commit()

    resp = auth_client.post(
        f"/settings/recipients/{recipient.id}/remove", follow_redirects=True,
    )
    assert resp.status_code == 200

    db.session.refresh(summary)
    assert summary.params.get("send_email") is False
    assert EditionRecipient.query.filter_by(id=recipient.id).first() is None


def test_remove_confirmed_recipient_sends_notification(auth_client, db, user, app, monkeypatch):
    recipient = EditionRecipient(user_id=user.id, email="friend@example.com")
    db.session.add(recipient)
    db.session.commit()
    recipient.confirmed_at = recipient.created_at
    db.session.commit()

    sent = []
    monkeypatch.setattr(
        edition_mail, "send_via_newsletter_mailbox",
        lambda to, subject, body: sent.append(to) or True,
    )

    auth_client.post(f"/settings/recipients/{recipient.id}/remove", follow_redirects=True)
    assert sent == ["friend@example.com"]


def test_remove_pending_recipient_sends_no_notification(auth_client, db, user, monkeypatch):
    recipient = EditionRecipient(user_id=user.id, email="friend@example.com", confirm_token="t1")
    db.session.add(recipient)
    db.session.commit()

    sent = []
    monkeypatch.setattr(
        edition_mail, "send_via_newsletter_mailbox",
        lambda to, subject, body: sent.append(to) or True,
    )

    auth_client.post(f"/settings/recipients/{recipient.id}/remove", follow_redirects=True)
    assert sent == []


def test_cannot_remove_another_users_recipient(auth_client, db, user, admin):
    other_recipient = EditionRecipient(user_id=admin.id, email=admin.email, confirmed_at=admin.created_at)
    db.session.add(other_recipient)
    db.session.commit()

    resp = auth_client.post(f"/settings/recipients/{other_recipient.id}/remove")
    assert resp.status_code == 403


def test_send_via_newsletter_mailbox_logs_when_unconfigured(app):
    with app.app_context():
        app.config["IMAP_SMTP_HOST"] = ""
        assert edition_mail.send_via_newsletter_mailbox("x@example.com", "Subject", "Body") is False
