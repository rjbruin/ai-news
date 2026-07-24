import pytest

from app.agent import memory as agent_memory
from app.models import Summary, SummaryRun, User


@pytest.fixture
def system_dispatch(db, admin):
    """The admin's Dispatch: the System Dispatch, published as its directory entry."""
    s = Summary(
        user_id=admin.id, name="Admin Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={"release_time": "09:00"},
        is_system_dispatch=True, is_published=True, published_name="AI Tech Dispatch",
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


# ── Registration defaults to following the system dispatch ─────────────────

def test_registration_follows_system_dispatch(client, db, system_dispatch):
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
    assert new_user.is_following(system_dispatch)


# ── Follow / unfollow ───────────────────────────────────────────────────────

def test_follow_any_published_dispatch(auth_client, db, user, system_dispatch):
    resp = auth_client.post("/dispatch/follow", data={"summary_id": system_dispatch.id})
    assert resp.status_code == 302
    db.session.refresh(user)
    assert user.is_following(system_dispatch)


def test_follow_is_idempotent(auth_client, db, user, system_dispatch):
    auth_client.post("/dispatch/follow", data={"summary_id": system_dispatch.id})
    auth_client.post("/dispatch/follow", data={"summary_id": system_dispatch.id})
    assert user.subscribed_dispatches.count() == 1


def test_unfollow(auth_client, db, user, system_dispatch):
    user.follow(system_dispatch)
    db.session.commit()
    auth_client.post("/dispatch/unfollow", data={"summary_id": system_dispatch.id})
    db.session.refresh(user)
    assert not user.is_following(system_dispatch)


def test_cannot_follow_unpublished_other_dispatch(auth_client, db, user, admin):
    other = Summary(user_id=admin.id, name="Private", type_key="agentic_page", params={})
    db.session.add(other)
    db.session.commit()
    resp = auth_client.post("/dispatch/follow", data={"summary_id": other.id})
    assert resp.status_code == 404
    assert not user.is_following(other)


def test_follow_rejects_disabled_and_missing(auth_client, db, system_dispatch):
    system_dispatch.enabled = False
    db.session.commit()
    assert auth_client.post("/dispatch/follow", data={"summary_id": system_dispatch.id}).status_code == 404
    assert auth_client.post("/dispatch/follow", data={"summary_id": 999999}).status_code == 404


def test_follow_bulk_sets_exact_set(auth_client, db, user, admin, system_dispatch):
    other = Summary(
        user_id=admin.id, name="Other", type_key="agentic_page", params={},
        is_published=True, published_name="Other Pub",
    )
    db.session.add(other)
    db.session.commit()
    user.follow(system_dispatch)
    db.session.commit()

    # Follow only `other`, dropping the system dispatch.
    resp = auth_client.post("/dispatch/follow-bulk", data={"summary_id": [other.id]})
    assert resp.status_code == 204
    db.session.refresh(user)
    assert user.is_following(other)
    assert not user.is_following(system_dispatch)


def test_follow_bulk_never_drops_own_dispatch(auth_client, db, user, system_dispatch):
    own = Summary(
        user_id=user.id, name="Mine", type_key="agentic_page", params={},
        is_published=True, published_name="Mine Pub",
    )
    db.session.add(own)
    db.session.commit()
    user.follow(own)
    db.session.commit()

    # Bulk-set to empty — own must survive.
    auth_client.post("/dispatch/follow-bulk", data={})
    db.session.refresh(user)
    assert user.is_following(own)


# ── Publishing ──────────────────────────────────────────────────────────────

@pytest.fixture
def own_client(auth_client, db, user):
    """auth_client whose user owns a Dispatch."""
    s = Summary(user_id=user.id, name="Alice Daily", type_key="agentic_page", params={})
    db.session.add(s)
    db.session.commit()
    return auth_client


def test_publish_own_dispatch(own_client, db, user):
    resp = own_client.post(
        "/dispatch/publish",
        data={"is_published": "on", "published_name": "Alice's AI News"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    own = user.own_dispatch
    assert own.is_published
    assert own.published_name == "Alice's AI News"


def test_publish_rejects_too_long_name(own_client, db, user):
    own_client.post(
        "/dispatch/publish",
        data={"is_published": "on", "published_name": "x" * 26},
    )
    assert not user.own_dispatch.is_published


def test_publish_rejects_duplicate_name(own_client, db, user, system_dispatch):
    # system_dispatch is published as "AI Tech Dispatch".
    own_client.post(
        "/dispatch/publish",
        data={"is_published": "on", "published_name": "ai tech dispatch"},  # case-insensitive clash
    )
    assert not user.own_dispatch.is_published


def test_unpublish(own_client, db, user):
    own_client.post("/dispatch/publish", data={"is_published": "on", "published_name": "Pub"})
    own_client.post("/dispatch/publish", data={})  # is_published absent = unpublish
    assert not user.own_dispatch.is_published


def test_publish_requires_own_dispatch(auth_client):
    resp = auth_client.post("/dispatch/publish", data={"is_published": "on", "published_name": "X"})
    assert resp.status_code == 404


# ── dispatch_own ─────────────────────────────────────────────────────────────

def test_dispatch_own_creates_clones_and_follows(auth_client, db, user, system_dispatch):
    agent_memory.write(system_dispatch.user, system_dispatch, "content_config", "SYSTEM CONTENT CONFIG")
    agent_memory.write(system_dispatch.user, system_dispatch, "interests", "SYSTEM INTERESTS")

    resp = auth_client.post("/dispatch/own", follow_redirects=True)
    assert resp.status_code == 200

    own = user.own_dispatch
    assert own is not None
    assert own.period == system_dispatch.period
    db.session.refresh(user)
    assert user.is_following(own)
    assert agent_memory.read(user, own, "content_config") == "SYSTEM CONTENT CONFIG"
    assert agent_memory.read(user, own, "interests") == "SYSTEM INTERESTS"


def test_dispatch_own_second_call_does_not_duplicate(auth_client, db, user, system_dispatch):
    auth_client.post("/dispatch/own")
    first = user.own_dispatch
    auth_client.post("/dispatch/own")
    all_owned = Summary.query.filter_by(user_id=user.id, type_key="agentic_page").all()
    assert len(all_owned) == 1 and all_owned[0].id == first.id


def test_dispatch_own_drops_the_admins_model_override(auth_client, db, user, system_dispatch):
    system_dispatch.params = {"release_time": "09:00", "model": "admin-only-model"}
    db.session.commit()
    auth_client.post("/dispatch/own")
    assert "model" not in (user.own_dispatch.params or {})


# ── Read-path relaxation (following) ────────────────────────────────────────

def test_follower_can_view_edition(auth_client, db, user, system_dispatch, system_run):
    user.follow(system_dispatch)
    db.session.commit()
    resp = auth_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}")
    assert resp.status_code == 200


def test_non_follower_cannot_view_edition(auth_client, system_dispatch, system_run):
    resp = auth_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}")
    assert resp.status_code == 403


def test_owner_can_always_view_own_edition(admin_client, system_dispatch, system_run):
    resp = admin_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}")
    assert resp.status_code == 200


def test_editions_feed_merges_followed_dispatches(auth_client, db, user, system_dispatch, system_run):
    user.follow(system_dispatch)
    db.session.commit()
    resp = auth_client.get("/summaries")
    assert resp.status_code == 200
    assert f"/summaries/{system_dispatch.id}/editions/{system_run.id}".encode() in resp.data


def test_serve_edition_pdf_allows_follower(auth_client, db, user, system_dispatch, system_run):
    import os
    from flask import current_app

    user.follow(system_dispatch)
    system_run.pdf_file = f"edition_{system_run.id}.pdf"
    db.session.commit()
    with auth_client.application.app_context():
        pdf_dir = os.path.join(current_app.instance_path, "pdfs")
        os.makedirs(pdf_dir, exist_ok=True)
        with open(os.path.join(pdf_dir, system_run.pdf_file), "wb") as fh:
            fh.write(b"%PDF-1.4 fake")
    resp = auth_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}/pdf")
    assert resp.status_code == 200


def test_serve_edition_pdf_blocks_non_follower(auth_client, db, system_dispatch, system_run):
    system_run.pdf_file = f"edition_{system_run.id}.pdf"
    db.session.commit()
    resp = auth_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}/pdf")
    assert resp.status_code == 403


# ── Write-path stays blocked for followers who don't own ───────────────────

@pytest.mark.parametrize("suffix,method", [
    ("/feedback", "post"),
    ("/feedback/save", "post"),
    ("/delete", "post"),
])
def test_mutations_blocked_for_follower(auth_client, db, user, system_dispatch, system_run, suffix, method):
    user.follow(system_dispatch)
    db.session.commit()
    url = f"/summaries/{system_dispatch.id}/editions/{system_run.id}{suffix}"
    resp = getattr(auth_client, method)(url, data={"feedback": "x"})
    assert resp.status_code == 403


def test_summary_delete_blocked_for_follower(auth_client, db, user, system_dispatch):
    user.follow(system_dispatch)
    db.session.commit()
    resp = auth_client.post(f"/summaries/{system_dispatch.id}/delete")
    assert resp.status_code == 403
    assert Summary.query.get(system_dispatch.id) is not None


def test_summary_delete_clears_follows(admin_client, db, admin, user, system_dispatch):
    user.follow(system_dispatch)
    db.session.commit()
    admin_client.post(f"/summaries/{system_dispatch.id}/delete")
    assert Summary.query.get(system_dispatch.id) is None
    db.session.refresh(user)
    assert user.subscribed_dispatches.count() == 0


# ── Dispatches directory + detail ──────────────────────────────────────────

def test_dispatches_directory_lists_published(auth_client, system_dispatch):
    resp = auth_client.get("/dispatches")
    assert resp.status_code == 200
    assert b"AI Tech Dispatch" in resp.data


def test_dispatch_detail_readable_when_published(auth_client, system_dispatch, system_run):
    resp = auth_client.get(f"/dispatches/{system_dispatch.id}")
    assert resp.status_code == 200
    assert b"AI Tech Dispatch" in resp.data


def test_dispatch_detail_404_when_unpublished_and_not_following(auth_client, db, admin):
    private = Summary(user_id=admin.id, name="Priv", type_key="agentic_page", params={})
    db.session.add(private)
    db.session.commit()
    resp = auth_client.get(f"/dispatches/{private.id}")
    assert resp.status_code == 404


# ── Template: read-only viewer sees the "set up your own" CTA ─────────────

def test_view_shows_setup_own_dispatch_cta_for_follower(auth_client, db, user, system_dispatch, system_run):
    user.follow(system_dispatch)
    db.session.commit()
    html = auth_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}").data.decode()
    assert "Set up my own Dispatch" in html
    assert "Give feedback to the editor" not in html
    assert "Delete edition" not in html


def test_view_shows_real_feedback_box_for_owner(admin_client, system_dispatch, system_run):
    html = admin_client.get(f"/summaries/{system_dispatch.id}/editions/{system_run.id}").data.decode()
    assert "Give feedback to the editor" in html
    assert "Delete edition" in html


# ── Onboarding ──────────────────────────────────────────────────────────────

def test_onboarding_modal_includes_follow_step(auth_client, db, user, system_dispatch):
    user.has_seen_onboarding = False
    db.session.commit()
    html = auth_client.get("/dashboard").data.decode()
    assert "Follow some Dispatches" in html
    assert "AI Tech Dispatch" in html
    assert html.count("onboarding-dot") >= 6


# ── Part A: "dispatch user" gating (topics / sources / PDF) ────────────────

def _make_user_own(db, user):
    s = Summary(user_id=user.id, name="Mine", type_key="agentic_page", params={})
    db.session.add(s)
    db.session.commit()
    return s


def test_topics_add_button_hidden_for_non_dispatch_user(auth_client, db, user):
    html = auth_client.get("/topics").data.decode()
    assert "Add a topic" not in html
    assert "Your topics" not in html


def test_topics_add_button_shown_for_dispatch_user(auth_client, db, user):
    _make_user_own(db, user)
    html = auth_client.get("/topics").data.decode()
    assert "Add a topic" in html
    assert "Your topics" in html


def test_topic_create_forbidden_for_non_dispatch_user(auth_client, db, user):
    resp = auth_client.post("/topics/create", data={"name": "X"})
    assert resp.status_code == 403


def test_topic_create_allowed_for_dispatch_user(auth_client, db, user):
    _make_user_own(db, user)
    resp = auth_client.post("/topics/create", data={"name": "MyTopic"}, follow_redirects=True)
    assert resp.status_code == 200
    from app.models import Tag
    assert Tag.query.filter_by(name="MyTopic").first() is not None


def test_sources_in_my_editions_column_hidden_for_non_dispatch_user(auth_client, db, user):
    html = auth_client.get("/sources").data.decode()
    assert "In my editions" not in html


def test_sources_in_my_editions_column_shown_for_dispatch_user(auth_client, db, user):
    _make_user_own(db, user)
    html = auth_client.get("/sources").data.decode()
    assert "In my editions" in html


def test_source_toggle_mine_forbidden_for_non_dispatch_user(auth_client, db, user):
    from app.models import Source
    src = Source(type_key="rss", name="S", config={}, enabled=True)
    db.session.add(src)
    db.session.commit()
    resp = auth_client.post(f"/sources/{src.id}/toggle-mine")
    assert resp.status_code == 403


def test_pdf_settings_hidden_for_non_dispatch_user(auth_client, db, user):
    html = auth_client.get("/settings").data.decode()
    assert 'id="sec-pdf"' not in html


def test_pdf_settings_shown_for_dispatch_user(auth_client, db, user):
    _make_user_own(db, user)
    html = auth_client.get("/settings").data.decode()
    assert 'id="sec-pdf"' in html


# ── System dispatch lookup + admin toggle (unchanged behavior) ─────────────

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
