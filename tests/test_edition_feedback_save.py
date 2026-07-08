import json

import pytest

from app.agent import memory as agent_memory
from app.models import Summary
from app.services import summarize

from conftest import give_edition_key


@pytest.fixture
def keyed_summary(db, user):
    give_edition_key(db, user, "sk-or-test", "test/model")
    s = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day",
        params={"release_days": [0, 1, 2, 3, 4, 5, 6]},
    )
    db.session.add(s)
    db.session.commit()
    return s


def _chat(messages, *, tools=None, api_key=None, model=None, **kw):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "c1", "type": "function",
        "function": {"name": "set_document", "arguments": json.dumps({"blocks": [
            {"type": "edition_header", "title": "Daily"},
        ]})},
    }], "_usage": {}}


def test_save_feedback_does_not_trigger_a_revision(auth_client, db, user, keyed_summary, monkeypatch):
    monkeypatch.setattr("app.agent.runner.openrouter.chat", _chat)
    _, _i, run = summarize.build_summary(keyed_summary, record_run=True)

    # If a revision were started, this would be called again — assert it never is.
    calls = {"n": 0}

    def _fail_if_called(*a, **kw):
        calls["n"] += 1
        return _chat(*a, **kw)

    monkeypatch.setattr("app.agent.runner.openrouter.chat", _fail_if_called)

    resp = auth_client.post(
        f"/summaries/{keyed_summary.id}/editions/{run.id}/feedback/save",
        data={"feedback": "Lead with research papers, drop crypto entirely."},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert calls["n"] == 0
    assert b"Saved" in resp.data


def test_save_feedback_appends_to_interests_memory(auth_client, db, user, keyed_summary, monkeypatch):
    monkeypatch.setattr("app.agent.runner.openrouter.chat", _chat)
    _, _i, run = summarize.build_summary(keyed_summary, record_run=True)

    auth_client.post(
        f"/summaries/{keyed_summary.id}/editions/{run.id}/feedback/save",
        data={"feedback": "Drop crypto coverage entirely."},
    )

    content = agent_memory.read(user, keyed_summary, "interests")
    assert "Drop crypto coverage entirely." in content
    assert "Unreviewed feedback" in content


def test_save_feedback_appends_multiple_notes_under_one_section(auth_client, db, user, keyed_summary, monkeypatch):
    monkeypatch.setattr("app.agent.runner.openrouter.chat", _chat)
    _, _i, run = summarize.build_summary(keyed_summary, record_run=True)

    auth_client.post(
        f"/summaries/{keyed_summary.id}/editions/{run.id}/feedback/save",
        data={"feedback": "First note."},
    )
    auth_client.post(
        f"/summaries/{keyed_summary.id}/editions/{run.id}/feedback/save",
        data={"feedback": "Second note."},
    )

    content = agent_memory.read(user, keyed_summary, "interests")
    assert content.count("Unreviewed feedback") == 1
    assert "First note." in content
    assert "Second note." in content


def test_save_feedback_requires_text(auth_client, db, user, keyed_summary, monkeypatch):
    monkeypatch.setattr("app.agent.runner.openrouter.chat", _chat)
    _, _i, run = summarize.build_summary(keyed_summary, record_run=True)

    resp = auth_client.post(
        f"/summaries/{keyed_summary.id}/editions/{run.id}/feedback/save",
        data={"feedback": "   "},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Enter some feedback first" in resp.data


def test_save_feedback_requires_ownership(auth_client, db, admin, keyed_summary, monkeypatch):
    monkeypatch.setattr("app.agent.runner.openrouter.chat", _chat)
    _, _i, run = summarize.build_summary(keyed_summary, record_run=True)

    other_summary = Summary(
        user_id=admin.id, name="Admin's", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(other_summary)
    db.session.commit()

    resp = auth_client.post(
        f"/summaries/{other_summary.id}/editions/{run.id}/feedback/save",
        data={"feedback": "x"},
    )
    assert resp.status_code == 403
