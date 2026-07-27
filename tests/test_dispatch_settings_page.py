from app.models import Summary


def _own_dispatch(db, user):
    s = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(s)
    db.session.commit()
    return s


def test_nav_hides_dispatch_link_when_none(auth_client, db, user):
    html = auth_client.get("/dashboard").data.decode()
    assert "Set up your Dispatch" not in html
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


def test_api_keys_hidden_for_user_without_dispatch(auth_client, db, user):
    # Without a Dispatch there's nothing an API key would fund, so the
    # section is hidden entirely rather than shown pre-emptively.
    resp = auth_client.get("/dispatch/settings")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert 'id="sec-api-keys"' not in html
    assert "Set up my own Dispatch" in html


def test_api_keys_reachable_once_dispatch_owned(auth_client, db, user):
    _own_dispatch(db, user)
    resp = auth_client.get("/dispatch/settings")
    assert resp.status_code == 200
    assert b'id="sec-api-keys"' in resp.data


def test_settings_page_no_longer_shows_dispatch_or_api_keys(auth_client, db, user):
    _own_dispatch(db, user)
    html = auth_client.get("/settings").data.decode()
    assert 'id="sec-dispatch"' not in html
    assert 'id="sec-api-keys"' not in html
    assert 'id="sec-schedule"' not in html
    assert 'id="sec-recipients"' in html


def test_dispatches_page_shows_setup_card_without_own_dispatch(auth_client, db, user):
    html = auth_client.get("/dispatches").data.decode()
    assert "Set up your own Dispatch" in html
    assert "Configure your own agentic editor" in html
    assert 'id="own-dispatch-explainer"' in html


def test_dispatches_page_hides_setup_card_with_own_dispatch(auth_client, db, user):
    _own_dispatch(db, user)
    html = auth_client.get("/dispatches").data.decode()
    assert "Set up your own Dispatch" not in html


def test_dispatches_page_setup_card_is_last(auth_client, db, admin):
    """A published Dispatch (admin's) plus the viewer's own setup CTA — the
    CTA must render after the real Dispatch cards, not before."""
    published = Summary(
        user_id=admin.id, name="Admin's", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
        is_published=True, published_name="Cool Dispatch",
    )
    db.session.add(published)
    db.session.commit()

    html = auth_client.get("/dispatches").data.decode()
    assert html.index("Cool Dispatch") < html.index("Set up your own Dispatch")


def test_dispatches_page_setup_card_does_not_submit_directly(auth_client, db, user):
    html = auth_client.get("/dispatches").data.decode()
    assert f'href="{"/dispatch/settings"}"' in html
    assert "dispatch-own-form" not in html


def test_dashboard_set_up_my_own_links_to_dispatch_settings(auth_client, db, user):
    html = auth_client.get("/dashboard").data.decode()
    assert "Set up my own" in html
    assert f'href="{"/dispatch/settings"}"' in html


def test_set_up_own_dispatch_opens_onboarding_modal_not_direct_submit(auth_client, db, user):
    html = auth_client.get("/dispatch/settings").data.decode()
    assert 'data-bs-target="#own-dispatch-onboarding-modal"' in html
    assert 'id="own-dispatch-onboarding-modal"' in html


def test_dispatch_settings_shows_restart_onboarding_button_with_dispatch(auth_client, db, user):
    _own_dispatch(db, user)
    html = auth_client.get("/dispatch/settings").data.decode()
    assert "Restart own Dispatch onboarding" in html
    # The generic intro copy is replaced, not shown alongside the button.
    assert "A Dispatch is an AI-written publication" not in html


def test_dispatch_settings_shows_intro_not_restart_button_without_dispatch(auth_client, db, user):
    html = auth_client.get("/dispatch/settings").data.decode()
    assert "A Dispatch is an AI-written publication" in html
    assert "Restart own Dispatch onboarding" not in html


def test_own_dispatch_onboarding_includes_publishing_step(auth_client, db, user):
    html = auth_client.get("/dispatch/settings").data.decode()
    assert "Publishing your Dispatch" in html


def test_restart_onboarding_resets_flag_and_redirects(auth_client, db, user):
    user.has_seen_onboarding = True
    db.session.commit()

    resp = auth_client.post("/settings/restart-onboarding", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/dashboard")
    db.session.refresh(user)
    assert user.has_seen_onboarding is False


def test_settings_has_restart_onboarding_button(auth_client, db, user):
    html = auth_client.get("/settings").data.decode()
    assert "Restart onboarding" in html
    assert 'action="/settings/restart-onboarding"' in html
