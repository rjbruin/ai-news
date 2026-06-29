import json

import pytest

from app.agent import memory
from app.models import Summary, SummaryRun, User
from app.services import summarize
from app.summaries import registry as summary_registry


@pytest.fixture
def keyed_user(db):
    u = User(username="ed", email="ed@example.com", email_verified=True)
    u.set_password("pw")
    u.set_openrouter_key("sk-or-test")
    u.openrouter_model = "test/model"
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def agentic_summary(db, keyed_user):
    s = Summary(
        user_id=keyed_user.id, name="AI Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={"release_days": [0, 1, 2, 3, 4, 5, 6]},
    )
    db.session.add(s)
    db.session.commit()
    return s


def _scripted_chat():
    """Two tool turns (set_document, write_headlines), then a stop turn."""
    state = {"n": 0}

    def chat(messages, *, tools=None, api_key=None, model=None, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "set_document", "arguments": json.dumps({"blocks": [
                    {"type": "edition_header", "title": "AI Daily", "date": "Monday June 29"},
                    {"type": "intro", "markdown": "A busy day in AI."},
                    {"type": "story", "headline": "Big release", "dek": "It shipped.",
                     "emphasis": "lead", "url": "https://x.test"},
                ]})},
            }], "_usage": {"total_tokens": 200}}
        if state["n"] == 2:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c2", "type": "function",
                "function": {"name": "write_headlines",
                             "arguments": json.dumps({"notes": "- Big release"})},
            }], "_usage": {"total_tokens": 30}}
        return {"role": "assistant", "content": "Edition complete.", "_usage": {"total_tokens": 5}}

    return chat


def test_plugin_registered_and_agentic(app):
    types = summary_registry.all_types()
    assert "agentic_page" in types
    assert types["agentic_page"].is_agentic is True


def test_build_agentic_summary_end_to_end(monkeypatch, db, keyed_user, agentic_summary):
    monkeypatch.setattr("app.agent.runner.openrouter.chat", _scripted_chat())

    artifact, items, run = summarize.build_summary(agentic_summary, record_run=True)

    # Document stored as IR + rendered HTML content.
    assert run.document is not None
    assert any(b["type"] == "edition_header" for b in run.document)
    assert "AI Daily" in run.content
    assert 'href="https://x.test"' in run.content
    assert run.revision == 1
    assert run.parent_run_id is None

    # Headlines persisted against the edition timestamp.
    rows = memory.recent_headlines(keyed_user, agentic_summary, days=7)
    assert len(rows) == 1
    assert "Big release" in rows[0].content


def test_build_agentic_without_key_raises(monkeypatch, db, agentic_summary):
    from app.agent.creds import MissingCredentials
    # Remove the user's key.
    agentic_summary.user.set_openrouter_key(None)
    db.session.commit()
    with pytest.raises(MissingCredentials):
        summarize.build_summary(agentic_summary, record_run=True)


def test_defaults_seeded_on_first_run(monkeypatch, db, keyed_user, agentic_summary):
    monkeypatch.setattr("app.agent.runner.openrouter.chat", _scripted_chat())
    summarize.build_summary(agentic_summary, record_run=True)
    # compose_system_prompt seeds interests + content_config lazily.
    assert memory.read(keyed_user, agentic_summary, "interests")
    assert memory.read(keyed_user, agentic_summary, "content_config")


def _scripted_chat_with_history():
    """Agent builds a doc, appends HISTORY, then writes HEADLINES."""
    state = {"n": 0}

    def chat(messages, *, tools=None, api_key=None, model=None, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "set_document", "arguments": json.dumps({"blocks": [
                    {"type": "edition_header", "title": "AI Daily"},
                    {"type": "story", "headline": "Reasoning models surge", "emphasis": "lead"},
                ]})},
            }], "_usage": {}}
        if state["n"] == 2:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c2", "type": "function",
                "function": {"name": "append_history",
                             "arguments": json.dumps({"note": "Trend: reasoning models gaining ground."})},
            }], "_usage": {}}
        if state["n"] == 3:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c3", "type": "function",
                "function": {"name": "write_headlines",
                             "arguments": json.dumps({"notes": "- Reasoning models surge"})},
            }], "_usage": {}}
        return {"role": "assistant", "content": "done", "_usage": {}}

    return chat


def test_agent_writes_history_and_headlines(monkeypatch, db, keyed_user, agentic_summary):
    monkeypatch.setattr("app.agent.runner.openrouter.chat", _scripted_chat_with_history())
    summarize.build_summary(agentic_summary, record_run=True)

    history = memory.read(keyed_user, agentic_summary, "history")
    assert "reasoning models gaining ground" in history.lower()
    headlines = memory.recent_headlines(keyed_user, agentic_summary, days=7)
    assert len(headlines) == 1


def test_prune_respects_retention_window(db, keyed_user, agentic_summary):
    from datetime import timedelta
    from app.models import utcnow
    now = utcnow().replace(tzinfo=None)
    memory.write_headlines(keyed_user, agentic_summary, now - timedelta(days=2), "recent")
    memory.write_headlines(keyed_user, agentic_summary, now - timedelta(days=8), "stale")
    # Default 7-day window removes only the 8-day-old row.
    assert memory.prune_headlines(days=7) == 1
    remaining = memory.recent_headlines(keyed_user, agentic_summary, days=30)
    assert [r.content for r in remaining] == ["recent"]
