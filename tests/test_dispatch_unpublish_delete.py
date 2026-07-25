from app.models import Summary, dispatch_email_subscriptions, dispatch_subscriptions


def _own_dispatch(db, user, published=False, name="Alice's"):
    s = Summary(
        user_id=user.id, name=name, type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
        is_published=published, published_name=("Alice Pub" if published else None),
    )
    db.session.add(s)
    db.session.commit()
    user.follow(s)
    db.session.commit()
    return s


def test_unpublish_removes_from_directory(auth_client, db, user):
    dispatch = _own_dispatch(db, user, published=True)
    resp = auth_client.post("/dispatch/publish", data={"is_published": ""}, follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(dispatch)
    assert dispatch.is_published is False
    assert dispatch not in Summary.published().all()


def test_delete_own_dispatch(auth_client, db, user, admin):
    dispatch = _own_dispatch(db, user)
    other_summary_id = dispatch.id
    admin.follow(dispatch)
    admin.subscribe_email(dispatch)
    db.session.commit()

    resp = auth_client.post(f"/summaries/{other_summary_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert db.session.get(Summary, other_summary_id) is None
    # Association rows for the deleted summary are gone too.
    assert db.session.execute(
        dispatch_subscriptions.select().where(dispatch_subscriptions.c.summary_id == other_summary_id)
    ).first() is None
    assert db.session.execute(
        dispatch_email_subscriptions.select().where(
            dispatch_email_subscriptions.c.summary_id == other_summary_id
        )
    ).first() is None


def test_delete_redirects_to_settings_for_own_dispatch(auth_client, db, user):
    dispatch = _own_dispatch(db, user)
    resp = auth_client.post(f"/summaries/{dispatch.id}/delete")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/settings")


def test_cannot_delete_another_users_dispatch(auth_client, db, user, admin):
    dispatch = _own_dispatch(db, admin)
    resp = auth_client.post(f"/summaries/{dispatch.id}/delete")
    assert resp.status_code == 403
    assert db.session.get(Summary, dispatch.id) is not None
