import json

import pytest

from app.agent import memory, prompt, runner, tools
from app.agent.context import AgentSession
from app.models import Summary, SummaryRun, User


@pytest.fixture
def agent_user(db):
    u = User(username="agent", email="agent@example.com", email_verified=True)
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def agent_summary(db, agent_user):
    s = Summary(
        user_id=agent_user.id, name="Daily AI", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(s)
    db.session.commit()
    return s


@pytest.fixture
def session(agent_user, agent_summary, sample_items):
    return AgentSession(
        user=agent_user, summary=agent_summary, items=sample_items,
        range_start=None, range_end=None,
    )


# ── Memory ──────────────────────────────────────────────────────────────────

def test_memory_write_read_singleton(db, agent_user, agent_summary):
    memory.write(agent_user, agent_summary, "history", "first note")
    assert memory.read(agent_user, agent_summary, "history") == "first note"
    memory.write(agent_user, agent_summary, "history", "replaced")
    assert memory.read(agent_user, agent_summary, "history") == "replaced"


def test_memory_interests_is_user_level(db, agent_user, agent_summary):
    memory.write(agent_user, agent_summary, "interests", "likes LLMs")
    from app.models import AgentMemory
    row = AgentMemory.query.filter_by(kind="interests").first()
    assert row.summary_id is None  # user-level, not summary-scoped


def test_memory_ensure_default(db, agent_user, agent_summary):
    out = memory.ensure_default(agent_user, agent_summary, "content_config", "DEFAULT")
    assert out == "DEFAULT"
    # second call keeps the existing value
    memory.write(agent_user, agent_summary, "content_config", "EDITED")
    assert memory.ensure_default(agent_user, agent_summary, "content_config", "DEFAULT") == "EDITED"


def test_headlines_recent_and_prune(db, agent_user, agent_summary):
    from datetime import timedelta
    from app.models import utcnow
    now = utcnow().replace(tzinfo=None)
    memory.write_headlines(agent_user, agent_summary, now, "today")
    memory.write_headlines(agent_user, agent_summary, now - timedelta(days=10), "old")
    recent = memory.recent_headlines(agent_user, agent_summary, days=7)
    assert [r.content for r in recent] == ["today"]
    assert memory.prune_headlines(days=7) == 1


# ── Tools ───────────────────────────────────────────────────────────────────

def test_editor_tools_build_document(session):
    r = json.loads(tools.dispatch("set_document", {"blocks": [
        {"type": "edition_header", "title": "Hi"},
        {"type": "divider"},
    ]}, session))
    assert r["block_count"] == 2

    r = json.loads(tools.dispatch("add_block", {"block": {"type": "quote", "text": "x"}}, session))
    bid = r["block_id"]
    assert len(session.document) == 3

    r = json.loads(tools.dispatch("update_block", {"block_id": bid, "fields": {"attribution": "me"}}, session))
    assert r["ok"]
    assert session.document[-1]["attribution"] == "me"

    r = json.loads(tools.dispatch("move_block", {"block_id": bid, "to_index": 0}, session))
    assert session.document[0]["id"] == bid

    r = json.loads(tools.dispatch("remove_block", {"block_id": bid}, session))
    assert len(session.document) == 2


def test_editor_rejects_invalid_block(session):
    r = json.loads(tools.dispatch("set_document", {"blocks": [{"type": "bogus"}]}, session))
    assert "error" in r
    assert session.document == []  # unchanged


def test_data_tools_scope_and_item(session, sample_items):
    r = json.loads(tools.dispatch("list_scope_items", {}, session))
    assert r["count"] == len(sample_items)
    item_id = sample_items[0].id
    r = json.loads(tools.dispatch("get_item", {"item_id": item_id}, session))
    assert r["id"] == item_id
    assert "summary_text" in r
    r = json.loads(tools.dispatch("get_item", {"item_id": 999999}, session))
    assert "error" in r


def test_data_tools_expose_item_topics(session, sample_items):
    item_id = sample_items[0].id
    session.item_tags = {item_id: ["Robotics", "Funding"]}

    r = json.loads(tools.dispatch("list_scope_items", {}, session))
    by_id = {i["id"]: i for i in r["items"]}
    assert by_id[item_id]["topics"] == ["Robotics", "Funding"]
    # An item with no entry in item_tags still gets a topics field, just empty.
    other_id = sample_items[1].id
    assert by_id[other_id]["topics"] == []

    r = json.loads(tools.dispatch("get_item", {"item_id": item_id}, session))
    assert r["topics"] == ["Robotics", "Funding"]


def test_memory_tools(session, agent_user, agent_summary):
    r = json.loads(tools.dispatch("write_memory", {"kind": "interests", "content": "robots"}, session))
    assert r["ok"]
    r = json.loads(tools.dispatch("read_memory", {"kind": "interests"}, session))
    assert r["content"] == "robots"
    json.loads(tools.dispatch("append_history", {"note": "n1"}, session))
    json.loads(tools.dispatch("append_history", {"note": "n2"}, session))
    assert "n1" in memory.read(agent_user, agent_summary, "history")
    assert "n2" in memory.read(agent_user, agent_summary, "history")
    r = json.loads(tools.dispatch("write_headlines", {"notes": "covered X"}, session))
    assert session.pending_headlines == "covered X"


def test_dispatch_unknown_tool(session):
    assert "error" in json.loads(tools.dispatch("nope", {}, session))


# ── Prompt ──────────────────────────────────────────────────────────────────

def test_compose_system_prompt_includes_memory(session, agent_user, agent_summary):
    memory.write(agent_user, agent_summary, "history", "PRIOR-TREND-NOTE")
    sp = prompt.compose_system_prompt(agent_user, agent_summary)
    assert "editor" in sp.lower()
    assert "PRIOR-TREND-NOTE" in sp
    # defaults were seeded
    assert memory.read(agent_user, agent_summary, "interests")


# ── Runner (mocked LLM) ─────────────────────────────────────────────────────

def test_run_agent_drives_tools(monkeypatch, session):
    calls = {"n": 0}

    def fake_chat(messages, *, tools=None, api_key=None, model=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "set_document", "arguments": json.dumps({"blocks": [
                    {"type": "edition_header", "title": "AI Daily"},
                    {"type": "story", "headline": "Big news", "emphasis": "lead"},
                ]})},
            }], "_usage": {"total_tokens": 100}}
        if calls["n"] == 2:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c2", "type": "function",
                "function": {"name": "write_headlines", "arguments": json.dumps({"notes": "Big news"})},
            }], "_usage": {"total_tokens": 50}}
        return {"role": "assistant", "content": "Done.", "_usage": {"total_tokens": 10}}

    monkeypatch.setattr("app.agent.runner.openrouter.chat", fake_chat)
    doc = runner.run_agent(session, api_key="sk-test", model="test/model")

    assert calls["n"] == 3
    assert any(b["type"] == "edition_header" for b in doc)
    assert session.pending_headlines == "Big news"
    assert session.tokens_used == 160


def test_run_agent_empty_document_raises(monkeypatch, session):
    monkeypatch.setattr(
        "app.agent.runner.openrouter.chat",
        lambda *a, **k: {"role": "assistant", "content": "nothing", "_usage": {}},
    )
    with pytest.raises(runner.AgentError):
        runner.run_agent(session, api_key="sk", model="m")
