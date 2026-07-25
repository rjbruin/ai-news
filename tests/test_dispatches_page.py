from app.models import Summary


def _published(db, owner, name, published_name, description=None, params=None):
    s = Summary(
        user_id=owner.id, name=name, type_key="agentic_page",
        scope_mode="fixed_period", period="day", params=params or {},
        is_published=True, published_name=published_name, description=description,
    )
    db.session.add(s)
    db.session.commit()
    return s


def test_own_dispatch_listed_first_even_unpublished(auth_client, db, user, admin):
    _published(db, admin, "Admin's", "AAA First Alphabetically")
    own = Summary(
        user_id=user.id, name="Mine", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(own)
    db.session.commit()

    resp = auth_client.get("/dispatches")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert html.index("Mine") < html.index("AAA First Alphabetically")
    assert "unpublished" in html


def test_card_shows_description_and_schedule(auth_client, db, user, admin):
    _published(
        db, admin, "Admin's", "Cool Dispatch", description="News you can use.",
        params={"release_days": [0, 1, 2, 3], "release_time": "08:00"},
    )
    resp = auth_client.get("/dispatches")
    html = resp.data.decode()
    assert "News you can use." in html
    assert "every Monday-Thursday at 08:00 UTC" in html


def test_see_editions_button_present(auth_client, db, user, admin):
    _published(db, admin, "Admin's", "Cool Dispatch")
    resp = auth_client.get("/dispatches")
    assert b"See Editions" in resp.data
    assert b">Open<" not in resp.data


def test_details_button_only_for_published(auth_client, db, user):
    own = Summary(
        user_id=user.id, name="Mine", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(own)
    db.session.commit()
    resp = auth_client.get("/dispatches")
    assert b">Details<" not in resp.data
