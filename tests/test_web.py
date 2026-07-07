def test_index_ok(client):
    assert client.get("/").status_code == 200


def test_dashboard_requires_login(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 302


def test_non_admin_cannot_access_admin(auth_client):
    resp = auth_client.get("/admin/")
    assert resp.status_code == 403


def test_authenticated_pages_render(auth_client, sample_tags, sample_items):
    for path in ["/dashboard", "/news", "/summaries"]:
        assert auth_client.get(path).status_code == 200


def test_admin_pages_render(admin_client, sample_tags):
    assert admin_client.get("/admin/").status_code == 200
    assert admin_client.get("/admin/sources/new").status_code == 200


def test_create_and_view_summary(auth_client, db, user, sample_items):
    # "New summary" creation via a form was removed (single implicit
    # agentic_page summary per user); a Summary row is created directly here,
    # same as elsewhere in the app's current (admin/seed-provisioned) flow.
    from app.models import Summary

    summary = Summary(
        user_id=user.id, name="My daily", type_key="app_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()

    # Cut an edition, then view it via the edition URL.
    from app.services import summarize
    _, _, run = summarize.build_summary(summary, record_run=True)
    assert run is not None
    resp = auth_client.get(f"/summaries/{summary.id}/editions/{run.id}")
    assert resp.status_code == 200
