import pytest

from app.agent import memory as agent_memory
from app.models import Summary, SummaryRun, User
from conftest import give_edition_key


@pytest.fixture
def system_dispatch(db, admin):
    """The admin's Dispatch, marked as the System Dispatch."""
    s = Summary(
        user_id=admin.id, name="Admin Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={"release_time": "09:00"},
        is_system_dispatch=True,
    )
    db.session.add(s)
    db.session.commit()
    return s


@pytest.fixture
def system_run(db, system_dispatch):
    run = SummaryRun(
        summary_id=system_dispatch.id, label="Monday",
        document=[{"type": "intro", "markdown": "Hi."}],
        content="<p>Hi.</p>", status="ok",
    )
    db.session.add(run)
    db.session.commit()
    return run


# ── Registration defaults to the system dispatch ───────────────────────────

def test_registration_subscribes_new_user_to_system_dispatch(client, db, system_dispatch):
    from app.models import AdminSettings
    AdminSettings.get().registration_open = True
    db.session.commit()

    resp = client.post(
        "/auth/register",
        data={
            "username": "newkid", "email": "newkid@dispatch-users.test-domain.com",
            "password": "password123", "confirm": "password123",
            "submit": "Create account",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    new_user = User.query.filter_by(username="newkid").first()
    assert new_user is not None
    assert new_user.subscribed_summary_id == system_dispatch.id


# ── dispatch_subscribe ──────────────────────────────────────────────────────

def test_subscribe_to_any_enabled_dispatch_regardless_of_ownership(auth_client, db, user, system_dispatch):
    resp = auth_client.post(
        "/dispatch/subscribe", data={"summary_id": system_dispatch.id}, follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.subscribed_summary_id == system_dispatch.id


def test_subscribe_rejects_disabled_dispatch(auth_client, db, system_dispatch):
    system_dispatch.enabled = False
    db.session.commit()
    resp = auth_client.post("/dispatch/subscribe", data={"summary_id": system_dispatch.id})
    assert resp.status_code == 404


def test_subscribe_rejects_non_agentic_summary(auth_client, db, admin):
    s = Summary(user_id=admin.id, name="Debug", type_key="debug_agentic", params={})
    db.session.add(s)
    db.session.commit()
    resp = auth_client.post("/dispatch/subscribe", data={"summary_id": s.id})
    assert resp.status_code == 404


def test_subscribe_rejects_missing_summary(auth_client):
    resp = auth_client.post("/dispatch/subscribe", data={"summary_id": 999999})
    assert resp.status_code == 404


# ── dispatch_own ─────────────────────────────────────────────────────────────

def test_dispatch_own_creates_and_clones_from_system_dispatch(auth_client, db, user, system_dispatch):
    agent_memory.write(system_dispatch.user, system_dispatch, "content_config", "SYSTEM CONTENT CONFIG")
    agent_memory.write(system_dispatch.user, system_dispatch, "interests", "SYSTEM INTERESTS")

    resp = auth_client.post("/dispatch/own", follow_redirects=True)
    assert resp.status_code == 200

    own = Summary.query.filter_by(user_id=user.id, type_key="agentic_page").first()
    assert own is not None
    assert own.period == system_dispatch.period

    db.session.refresh(user)
    assert user.subscribed_summary_id == own.id
    assert agent_memory.read(user, own, "content_config") == "SYSTEM CONTENT CONFIG"
    assert agent_memory.read(user, own, "interests") == "SYSTEM INTERESTS"


def test_dispatch_own_does_not_clobber_existing_interests(auth_client, db, user, system_dispatch):
    agent_memory.write(user, system_dispatch, "interests", "ALICE'S OWN INTERESTS")
    agent_memory.write(system_dispatch.user, system_dispatch, "interests", "SYSTEM INTERESTS")

    auth_client.post("/dispatch/own")

    own = Summary.query.filter_by(user_id=user.id, type_key="agentic_page").first()
    assert agent_memory.read(user, own, "interests") == "ALICE'S OWN INTERESTS"


def test_dispatch_own_second_call_does_not_duplicate(auth_client, db, user, system_dispatch):
    auth_client.post("/dispatch/own")
    first = Summary.query.filter_by(user_id=user.id, type_key="agentic_page").first()

    auth_client.post("/dispatch/own")
    all_owned = Summary.query.filter_by(user_id=user.id, type_key="agentic_page").all()
    assert len(all_owned) == 1
    assert all_owned[0].id == first.id


def test_dispatch_own_drops_the_admins_model_override(auth_client, db, user, system_dispatch):
    system_dispatch.params = {"release_time": "09:00", "model": "admin-only-model"}
    db.session.commit()
    auth_client.post("/dispatch/own")
    own = Summary.query.filter_by(user_id=user.id, type_key="agentic_page").first()
    assert "model" not in (own.params or {})


# ── Read-path relaxation ────────────────────────────────────────────────────

def test_subscribed_non_owner_can_view_edition(auth_client, db, user, system_dispatch, system_run):
    user.subscribed_summary_id = system_dispatch.id
    db.session.commit()
    resp = auth_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}")
    assert resp.status_code == 200


def test_unsubscribed_non_owner_cannot_view_edition(auth_client, system_dispatch, system_run):
    resp = auth_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}")
    assert resp.status_code == 403


def test_owner_can_always_view_own_edition(admin_client, system_dispatch, system_run):
    resp = admin_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}")
    assert resp.status_code == 200


def test_summaries_list_shows_subscribed_non_owned_dispatch(auth_client, db, user, system_dispatch, system_run):
    user.subscribed_summary_id = system_dispatch.id
    db.session.commit()
    resp = auth_client.get("/summaries")
    assert resp.status_code == 200
    assert b"read only" in resp.data
    assert system_dispatch.user.username.encode() in resp.data


def test_serve_edition_pdf_allows_subscribed_non_owner(auth_client, db, user, system_dispatch, system_run):
    import os
    from flask import current_app

    user.subscribed_summary_id = system_dispatch.id
    system_run.pdf_file = f"edition_{system_run.id}.pdf"
    db.session.commit()

    with auth_client.application.app_context():
        pdf_dir = os.path.join(current_app.instance_path, "pdfs")
        os.makedirs(pdf_dir, exist_ok=True)
        with open(os.path.join(pdf_dir, system_run.pdf_file), "wb") as fh:
            fh.write(b"%PDF-1.4 fake")

    resp = auth_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}/pdf")
    assert resp.status_code == 200


def test_serve_edition_pdf_blocks_unsubscribed_non_owner(auth_client, db, system_dispatch, system_run):
    system_run.pdf_file = f"edition_{system_run.id}.pdf"
    db.session.commit()
    resp = auth_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}/pdf")
    assert resp.status_code == 403


# ── Write-path stays blocked for non-owners ────────────────────────────────

def test_edition_feedback_blocked_for_subscribed_non_owner(auth_client, db, user, system_dispatch, system_run):
    user.subscribed_summary_id = system_dispatch.id
    db.session.commit()
    resp = auth_client.post(
        f"/summaries/{system_dispatch.id}/editions/{system_run.id}/feedback",
        data={"feedback": "do something"},
    )
    assert resp.status_code == 403


def test_edition_feedback_save_blocked_for_subscribed_non_owner(auth_client, db, user, system_dispatch, system_run):
    user.subscribed_summary_id = system_dispatch.id
    db.session.commit()
    resp = auth_client.post(
        f"/summaries/{system_dispatch.id}/editions/{system_run.id}/feedback/save",
        data={"feedback": "do something"},
    )
    assert resp.status_code == 403


def test_edition_delete_blocked_for_subscribed_non_owner(auth_client, db, user, system_dispatch, system_run):
    user.subscribed_summary_id = system_dispatch.id
    db.session.commit()
    resp = auth_client.post(
        f"/summaries/{system_dispatch.id}/editions/{system_run.id}/delete",
    )
    assert resp.status_code == 403
    assert SummaryRun.query.get(system_run.id) is not None


def test_summary_delete_blocked_for_subscribed_non_owner(auth_client, db, user, system_dispatch):
    user.subscribed_summary_id = system_dispatch.id
    db.session.commit()
    resp = auth_client.post(f"/summaries/{system_dispatch.id}/delete")
    assert resp.status_code == 403
    assert Summary.query.get(system_dispatch.id) is not None


# ── Template: read-only viewer sees the "set up your own" CTA ─────────────

def test_view_shows_setup_own_dispatch_cta_for_non_owner(auth_client, db, user, system_dispatch, system_run):
    user.subscribed_summary_id = system_dispatch.id
    db.session.commit()
    resp = auth_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}")
    html = resp.data.decode()
    assert "Set up my own Dispatch" in html
    assert "Give feedback to the editor" not in html
    assert "Delete edition" not in html
    assert 'action="/dispatch/own"' in html


def test_view_shows_real_feedback_box_for_owner(admin_client, system_dispatch, system_run):
    resp = admin_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}")
    html = resp.data.decode()
    assert "Give feedback to the editor" in html
    assert "Delete edition" in html


# ── Onboarding step count ───────────────────────────────────────────────────

def test_onboarding_modal_includes_dispatch_step(auth_client, db, user):
    user.has_seen_onboarding = False
    db.session.commit()
    resp = auth_client.get("/dashboard")
    html = resp.data.decode()
    assert "You're reading the System Dispatch" in html
    assert html.count('class="onboarding-dot') == 5 or html.count("onboarding-dot") >= 5


# ── System dispatch lookup ──────────────────────────────────────────────────

def test_get_system_dispatch_returns_the_flagged_one(db, admin, user):
    s1 = Summary(user_id=admin.id, name="A", type_key="agentic_page", params={})
    s2 = Summary(user_id=user.id, name="B", type_key="agentic_page", params={}, is_system_dispatch=True)
    db.session.add_all([s1, s2])
    db.session.commit()
    assert Summary.get_system_dispatch().id == s2.id


def test_admin_can_change_system_dispatch(admin_client, db, admin, user):
    s1 = Summary(user_id=admin.id, name="A", type_key="agentic_page", params={}, is_system_dispatch=True)
    s2 = Summary(user_id=user.id, name="B", type_key="agentic_page", params={})
    db.session.add_all([s1, s2])
    db.session.commit()

    resp = admin_client.post(f"/admin/dispatches/{s2.id}/make-system", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(s1)
    db.session.refresh(s2)
    assert s1.is_system_dispatch is False
    assert s2.is_system_dispatch is True
