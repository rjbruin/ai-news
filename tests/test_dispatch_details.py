from app.agent import memory as agent_memory
from app.models import Summary, SummaryRun


def _published(db, owner, description="A test dispatch.", params=None):
    s = Summary(
        user_id=owner.id, name="Admin's", type_key="agentic_page",
        scope_mode="fixed_period", period="day",
        params=params or {"model": "anthropic/claude-3", "release_days": [0, 1], "release_time": "09:00"},
        is_published=True, published_name="Cool Dispatch", description=description,
    )
    db.session.add(s)
    db.session.commit()
    return s


def test_details_404_for_unpublished_non_own(auth_client, db, user, admin):
    own = Summary(
        user_id=admin.id, name="Admin's", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(own)
    db.session.commit()
    resp = auth_client.get(f"/dispatches/{own.id}/details")
    assert resp.status_code == 404


def test_details_200_for_own_unpublished(auth_client, db, user):
    own = Summary(
        user_id=user.id, name="Mine", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(own)
    db.session.commit()
    resp = auth_client.get(f"/dispatches/{own.id}/details")
    assert resp.status_code == 200


def test_details_shows_static_spec(auth_client, db, user, admin):
    dispatch = _published(db, admin)
    agent_memory.write(admin, dispatch, "interests", "loves robots")
    agent_memory.write(admin, dispatch, "content_config", "3 sections please")
    agent_memory.write(admin, dispatch, "history", "SECRET_RUNNING_NOTE")

    resp = auth_client.get(f"/dispatches/{dispatch.id}/details")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "A test dispatch." in html
    assert "every Monday-Tuesday at 09:00 UTC" in html
    assert "anthropic/claude-3" in html
    assert "loves robots" in html
    assert "3 sections please" in html
    # Excluded fields never leak onto the static spec page.
    assert "SECRET_RUNNING_NOTE" not in html
    assert "podcast_cost" not in html
    assert "pdf_export_enabled" not in html


def test_copy_to_own_creates_when_absent(auth_client, db, user, admin):
    dispatch = _published(db, admin)
    agent_memory.write(admin, dispatch, "content_config", "cloned content config")

    assert user.own_dispatch is None
    resp = auth_client.post(f"/dispatches/{dispatch.id}/copy-to-own", follow_redirects=True)
    assert resp.status_code == 200

    own = user.own_dispatch
    assert own is not None
    assert own.description == dispatch.description
    assert own.params.get("model") is None  # never copies model, matches dispatch_own's convention
    assert agent_memory.read(user, own, "content_config") == "cloned content config"
    assert user.is_following(own)


def test_copy_to_own_overwrites_existing(auth_client, db, user, admin):
    dispatch = _published(db, admin, description="Source description")
    own = Summary(
        user_id=user.id, name="Mine", type_key="agentic_page",
        scope_mode="fixed_period", period="week", params={"old": "value"},
        description="my old description",
    )
    db.session.add(own)
    db.session.commit()

    resp = auth_client.post(f"/dispatches/{dispatch.id}/copy-to-own", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(own)
    assert own.description == "Source description"
    assert own.period == "day"
    assert "old" not in (own.params or {})


def test_own_dispatch_details_shows_edit_settings_not_copy_button(auth_client, db, user):
    own = Summary(
        user_id=user.id, name="Mine", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(own)
    db.session.commit()

    resp = auth_client.get(f"/dispatches/{own.id}/details")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Copy configuration to my Dispatch" not in html
    assert html.count("Edit settings") == 2  # top-right and in place of the copy button


def test_other_dispatch_details_shows_copy_not_edit_settings(auth_client, db, user, admin):
    dispatch = _published(db, admin)
    resp = auth_client.get(f"/dispatches/{dispatch.id}/details")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Copy configuration to my Dispatch" in html
    assert "Edit settings" not in html
