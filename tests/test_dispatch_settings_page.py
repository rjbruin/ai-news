from app.models import Summary


def _own_dispatch(db, user):
    s = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(s)
    db.session.commit()
    return s


def test_nav_shows_set_up_your_dispatch_when_none(auth_client, db, user):
    html = auth_client.get("/dashboard").data.decode()
    assert "Set up your Dispatch" in html
    assert ">Your Dispatch<" not in html


def test_nav_shows_your_dispatch_when_owned(auth_client, db, user):
    _own_dispatch(db, user)
    html = auth_client.get("/dashboard").data.decode()
    assert ">Your Dispatch<" in html or "Your Dispatch\n" in html
    assert "Set up your Dispatch" not in html


def test_dispatch_settings_renders_for_user_with_no_dispatch(auth_client, db, user):
    resp = auth_client.get("/dispatch/settings")
    assert resp.status_code == 200
    assert b"Set up my own Dispatch" in resp.data


def test_dispatch_settings_renders_for_user_with_dispatch(auth_client, db, user):
    _own_dispatch(db, user)
    resp = auth_client.get("/dispatch/settings")
    assert resp.status_code == 200
    assert b"Set up my own Dispatch" not in resp.data
    assert b"Delete my Dispatch" in resp.data


def test_api_keys_reachable_for_any_user_without_dispatch(auth_client, db, user):
    # Managing API keys never requires admin approval or owning a Dispatch.
    resp = auth_client.get("/dispatch/settings")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert 'id="sec-api-keys"' in html
    # Still shows the CTA to set up a Dispatch alongside the API Keys card.
    assert "Set up my own Dispatch" in html


def test_settings_page_no_longer_shows_dispatch_or_api_keys(auth_client, db, user):
    _own_dispatch(db, user)
    html = auth_client.get("/settings").data.decode()
    assert 'id="sec-dispatch"' not in html
    assert 'id="sec-api-keys"' not in html
    assert 'id="sec-schedule"' not in html
    assert 'id="sec-recipients"' in html
