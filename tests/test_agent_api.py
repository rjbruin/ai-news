import json

import pytest

from app.models import Summary, SummaryRun
from app.services import summarize

from conftest import give_edition_key


@pytest.fixture
def keyed_summary(db, user):
    give_edition_key(db, user, "sk-or-test")
    s = Summary(
        user_id=user.id, name="API Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day",
        params={"release_days": [0, 1, 2, 3, 4, 5, 6]},
    )
    db.session.add(s)
    db.session.commit()
    return s


def _chat():
    state = {"n": 0}

    def chat(messages, *, tools=None, api_key=None, model=None, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "set_document", "arguments": json.dumps({"blocks": [
                    {"type": "edition_header", "title": "API Daily"},
                    {"type": "story", "headline": "Headline one", "emphasis": "lead"},
                ]})},
            }], "_usage": {}}
        return {"role": "assistant", "content": "done", "_usage": {}}

    return chat


def test_scope_items_and_item(auth_client, keyed_summary, sample_items):
    resp = auth_client.get(f"/api/summaries/{keyed_summary.id}/scope-items")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "items" in data and "count" in data

    item_id = sample_items[0].id
    resp = auth_client.get(f"/api/items/{item_id}")
    assert resp.status_code == 200
    assert resp.get_json()["id"] == item_id


def test_editions_endpoints(auth_client, db, keyed_summary, monkeypatch):
    monkeypatch.setattr("app.agent.runner.openrouter.chat", _chat())
    _, _i, run = summarize.build_summary(keyed_summary, record_run=True)

    resp = auth_client.get(f"/api/summaries/{keyed_summary.id}/editions")
    assert resp.status_code == 200
    eds = resp.get_json()["editions"]
    assert len(eds) == 1 and eds[0]["run_id"] == run.id

    resp = auth_client.get(f"/api/summaries/{keyed_summary.id}/editions/{run.id}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["document"]
    assert body["revisions"][0]["revision"] == 1


def test_memory_get_put(auth_client, keyed_summary):
    resp = auth_client.put(
        f"/api/summaries/{keyed_summary.id}/memory/interests",
        json={"content": "robots and LLMs"},
    )
    assert resp.status_code == 200
    resp = auth_client.get(f"/api/summaries/{keyed_summary.id}/memory/interests")
    assert resp.get_json()["content"] == "robots and LLMs"

    # Non-writable kind -> 404
    assert auth_client.get(f"/api/summaries/{keyed_summary.id}/memory/headlines").status_code == 404


def test_feedback_creates_revision(auth_client, db, keyed_summary, monkeypatch):
    monkeypatch.setattr("app.agent.runner.openrouter.chat", _chat())
    _, _i, run = summarize.build_summary(keyed_summary, record_run=True)

    # Agent makes one edit then stops.
    calls = {"n": 0}

    def chat(messages, *, tools=None, api_key=None, model=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "e", "type": "function",
                "function": {"name": "add_block", "arguments": json.dumps({"block": {"type": "divider"}})},
            }], "_usage": {}}
        return {"role": "assistant", "content": "done", "_usage": {}}

    monkeypatch.setattr("app.agent.runner.openrouter.chat", chat)
    resp = auth_client.post(
        f"/api/summaries/{keyed_summary.id}/editions/{run.id}/feedback",
        json={"text": "add a divider"},
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["revision"] == 2
    assert body["parent_run_id"] == run.id


def test_feedback_requires_text(auth_client, db, keyed_summary, monkeypatch):
    monkeypatch.setattr("app.agent.runner.openrouter.chat", _chat())
    _, _i, run = summarize.build_summary(keyed_summary, record_run=True)
    resp = auth_client.post(
        f"/api/summaries/{keyed_summary.id}/editions/{run.id}/feedback", json={"text": "  "}
    )
    assert resp.status_code == 400


def test_api_ownership_enforced(auth_client, db, admin):
    # A summary owned by someone else -> 403.
    other = Summary(user_id=admin.id, name="Theirs", type_key="agentic_page",
                    scope_mode="fixed_period", period="day", params={})
    db.session.add(other)
    db.session.commit()
    assert auth_client.get(f"/api/summaries/{other.id}/scope-items").status_code == 403


def test_api_requires_login(client, keyed_summary):
    resp = client.get(f"/api/summaries/{keyed_summary.id}/scope-items")
    assert resp.status_code in (301, 302, 401)
