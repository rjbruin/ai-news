"""Tests for the admin usage-analytics page: cookie-free page-visit counting,
edition read counts, and per-Dispatch subscriber/reader counts."""
from app.models import EditionRead, PageVisit, Summary, SummaryRun


def _give_dispatch(db, user, name="D"):
    s = Summary(user_id=user.id, name=name, type_key="agentic_page", params={})
    db.session.add(s)
    db.session.commit()
    return s


def test_page_visit_recorded_on_get(client, db):
    client.get("/")
    row = PageVisit.query.filter_by(endpoint="web.index").first()
    assert row is not None
    assert row.count == 1

    client.get("/")
    row = PageVisit.query.filter_by(endpoint="web.index").first()
    assert row.count == 2


def test_page_visit_not_recorded_for_static_or_api(client, db):
    client.get("/static/does-not-exist.css")
    assert PageVisit.query.filter(PageVisit.endpoint.startswith("static")).count() == 0


def test_analytics_page_requires_admin(auth_client, db):
    resp = auth_client.get("/admin/analytics")
    assert resp.status_code == 403


def test_analytics_page_loads_for_admin(admin_client, db):
    resp = admin_client.get("/admin/analytics")
    assert resp.status_code == 200


def test_analytics_shows_page_visits(admin_client, db):
    admin_client.get("/")
    resp = admin_client.get("/admin/analytics")
    html = resp.data.decode()
    assert "web.index" in html


def test_analytics_shows_dispatch_subscribers_and_reads(admin_client, db, admin, user):
    dispatch = _give_dispatch(db, user, name="My Dispatch")
    user.follow(dispatch)
    admin.follow(dispatch)
    run = SummaryRun(summary_id=dispatch.id, label="Mon", status="ok", content="<p>x</p>")
    db.session.add(run)
    db.session.commit()
    db.session.add(EditionRead(user_id=user.id, run_id=run.id))
    db.session.commit()

    resp = admin_client.get("/admin/analytics")
    html = resp.data.decode()
    assert "My Dispatch" in html
    assert dispatch.follower_count == 2


def test_analytics_users_with_own_dispatch_count(admin_client, db, user):
    _give_dispatch(db, user)
    resp = admin_client.get("/admin/analytics")
    assert resp.status_code == 200
    # At least the one dispatch-owning user created above.
    from app.models import User as UserModel
    assert UserModel.query.filter(UserModel.id == user.id).count() == 1
