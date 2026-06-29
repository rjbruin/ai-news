from app.models import Tag


def test_index_ok(client):
    assert client.get("/").status_code == 200


def test_dashboard_requires_login(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 302


def test_create_tag(auth_client, db):
    resp = auth_client.post(
        "/tags/new",
        data={"name": "Ethics", "keywords": "bias, fairness", "explanation": "AI ethics"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert Tag.query.filter_by(name="Ethics").first() is not None


def test_tag_tryout_page(auth_client, sample_items):
    # Results are streamed via SSE; the POST just renders the try-out form page.
    resp = auth_client.post(
        "/tags/try-out",
        data={"name": "Robots", "keywords": "robot, humanoid", "explanation": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"try-out" in resp.data.lower() or b"tag" in resp.data.lower()


def test_non_admin_cannot_access_admin(auth_client):
    resp = auth_client.get("/admin/")
    assert resp.status_code == 403


def test_authenticated_pages_render(auth_client, sample_tags, sample_items):
    for path in ["/dashboard", "/news", "/tags", "/tags/try-out", "/summaries"]:
        assert auth_client.get(path).status_code == 200


def test_admin_pages_render(admin_client, sample_tags):
    assert admin_client.get("/admin/").status_code == 200
    assert admin_client.get("/admin/sources/new").status_code == 200


def test_create_and_view_summary(auth_client, db, sample_items):
    auth_client.post(
        "/summaries/new",
        data={"name": "My daily", "type_key": "app_page",
              "scope_mode": "fixed_period", "period": "day"},
        follow_redirects=True,
    )
    from app.models import Summary, SummaryRun

    summary = Summary.query.filter_by(name="My daily").first()
    assert summary is not None

    # Cut an edition, then view it via the edition URL.
    from app.services import summarize
    _, _, run = summarize.build_summary(summary, record_run=True)
    assert run is not None
    resp = auth_client.get(f"/summaries/{summary.id}/editions/{run.id}")
    assert resp.status_code == 200
