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
            }], "_usage": {"total_tokens": 200, "cost": 0.001}}
        if state["n"] == 2:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c2", "type": "function",
                "function": {"name": "write_headlines",
                             "arguments": json.dumps({"notes": "- Big release"})},
            }], "_usage": {"total_tokens": 30, "cost": 0.0002}}
        return {"role": "assistant", "content": "Edition complete.",
                "_usage": {"total_tokens": 5, "cost": 0.00005}}

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

    # Agent log + total cost recorded against the run.
    assert run.agent_log is not None
    event_types = [e["type"] for e in run.agent_log]
    assert "usage" in event_types
    assert "stop" in event_types
    assert run.agent_cost == pytest.approx(0.001 + 0.0002 + 0.00005)

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


def test_revise_edition_creates_linked_revision(monkeypatch, db, keyed_user, agentic_summary):
    monkeypatch.setattr("app.agent.runner.openrouter.chat", _scripted_chat())
    _, _items, run = summarize.build_summary(agentic_summary, record_run=True)
    assert run.revision == 1

    # Feedback turn: agent edits the (seeded) draft, then stops.
    def feedback_chat(messages, *, tools=None, api_key=None, model=None, **kw):
        # The opening user message should carry the feedback instruction.
        assert any("feedback" in (m.get("content") or "").lower() for m in messages)
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": "f1", "type": "function",
            "function": {"name": "add_block", "arguments": json.dumps({"block": {
                "type": "callout", "variant": "note", "title": "Per feedback",
                "markdown": "Adjusted."}})},
        }], "_usage": {}}

    state = {"done": False}

    def chat(messages, *, tools=None, api_key=None, model=None, **kw):
        if not state["done"]:
            state["done"] = True
            return feedback_chat(messages, tools=tools, api_key=api_key, model=model)
        return {"role": "assistant", "content": "ok", "_usage": {}}

    monkeypatch.setattr("app.agent.runner.openrouter.chat", chat)
    rev = summarize.revise_edition(run, "Add a note about the feedback.")

    assert rev.id != run.id
    assert rev.parent_run_id == run.id
    assert rev.revision == 2
    assert rev.range_start == run.range_start and rev.range_end == run.range_end
    # Revision seeded from the parent's document, then extended.
    assert any(b["type"] == "callout" for b in rev.document)


def test_revision_chain_and_heads(monkeypatch, db, keyed_user, agentic_summary):
    monkeypatch.setattr("app.agent.runner.openrouter.chat", _scripted_chat())
    _, _items, run = summarize.build_summary(agentic_summary, record_run=True)

    # Deterministic two-call revision script: add a divider, then stop.
    calls = {"n": 0}

    def chat(messages, *, tools=None, api_key=None, model=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "d", "type": "function",
                "function": {"name": "add_block", "arguments": json.dumps({"block": {"type": "divider"}})},
            }], "_usage": {}}
        return {"role": "assistant", "content": "done", "_usage": {}}

    monkeypatch.setattr("app.agent.runner.openrouter.chat", chat)
    rev = summarize.revise_edition(run, "tweak")

    chain = summarize.revision_chain(rev)
    assert [r.revision for r in chain] == [1, 2]
    heads = summarize.edition_heads(agentic_summary)
    assert len(heads) == 1
    assert heads[0].id == rev.id  # head is the latest revision


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
