"""Tests for the AI-generated-content badge and its disclosure page."""
from app.models import Summary, SummaryRun


def _give_dispatch(db, user):
    s = Summary(user_id=user.id, name="D", type_key="agentic_page", params={})
    db.session.add(s)
    db.session.commit()
    return s


def test_ai_disclosure_page_loads_without_login(client):
    resp = client.get("/ai-disclosure")
    assert resp.status_code == 200
    assert b"AI Act" in resp.data


def test_frontpage_shows_ai_badge(client):
    html = client.get("/").data.decode()
    assert 'href="/ai-disclosure"' in html
    assert "ai-badge" in html


def test_footer_shows_ai_badge_on_every_page(client):
    html = client.get("/ai-disclosure").data.decode()
    assert html.count('href="/ai-disclosure"') >= 2  # header badge + footer badge


def test_edition_page_shows_ai_badge(auth_client, db, user):
    dispatch = _give_dispatch(db, user)
    run = SummaryRun(summary_id=dispatch.id, label="Mon", status="ok", content="<p>x</p>")
    db.session.add(run)
    db.session.commit()

    html = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}").data.decode()
    assert 'href="/ai-disclosure"' in html
    assert "ai-badge" in html
