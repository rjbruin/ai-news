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


def test_quick_hits_recent_and_prune(db, agent_user, agent_summary):
    from datetime import timedelta
    from app.models import utcnow
    now = utcnow().replace(tzinfo=None)
    memory.write_quick_hits(agent_user, agent_summary, now, [{"item_id": 1, "headline": "today's hit"}])
    memory.write_quick_hits(agent_user, agent_summary, now - timedelta(days=10), [{"item_id": 2, "headline": "old hit"}])
    recent = memory.recent_quick_hits(agent_user, agent_summary, days=7)
    assert [r["headline"] for r in recent] == ["today's hit"]
    assert recent[0]["item_id"] == 1
    assert memory.prune_quick_hits(days=7) == 1


def test_write_quick_hits_noop_when_empty(db, agent_user, agent_summary):
    from app.models import utcnow
    row = memory.write_quick_hits(agent_user, agent_summary, utcnow().replace(tzinfo=None), [])
    assert row is None
    assert memory.recent_quick_hits(agent_user, agent_summary, days=7) == []


def test_reconcile_content_config_rewrites_legacy_types():
    text = (
        "Merge near-duplicates into a single `cluster`. "
        "A `callout` (variant trend) when relevant. "
        "Leftover items go in `quick_hits`. Featured `story` needs a body."
    )
    new_text, changed = memory.reconcile_content_config(text)
    assert changed is True
    assert "`item`" in new_text
    assert "`trend`" in new_text
    assert "`more_news`" in new_text
    assert "`cluster`" not in new_text
    assert "`callout`" not in new_text
    assert "`quick_hits`" not in new_text
    assert "`story`" not in new_text


def test_reconcile_content_config_leaves_current_text_untouched():
    text = "Use `item` blocks with `sources`, and `trend` for patterns."
    new_text, changed = memory.reconcile_content_config(text)
    assert changed is False
    assert new_text == text


def test_reconcile_content_config_ignores_prose_outside_backticks():
    # "story" appears in prose (not a code span) — must not be rewritten,
    # since it isn't necessarily referring to the block type.
    text = "Tell a good story about the news, using item blocks."
    new_text, changed = memory.reconcile_content_config(text)
    assert changed is False
    assert new_text == text


def test_prune_history_trims_oversized_singleton_at_newline_boundary(db, agent_user, agent_summary):
    notes = [f"note-{i}: " + ("x" * 50) for i in range(20)]
    content = "\n".join(notes) + "\n"
    memory.write(agent_user, agent_summary, "history", content)
    assert len(content) > 500

    trimmed = memory.prune_history(max_chars=500)
    assert trimmed == 1

    result = memory.read(agent_user, agent_summary, "history")
    assert len(result) <= 500
    assert notes[-1] in result  # most recent note survives, whole and intact
    for note in notes[:5]:
        assert note not in result  # oldest notes dropped


def test_prune_history_leaves_short_content_untouched(db, agent_user, agent_summary):
    memory.write(agent_user, agent_summary, "history", "short note")
    assert memory.prune_history(max_chars=6000) == 0
    assert memory.read(agent_user, agent_summary, "history") == "short note"


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


def test_add_block_overrides_sources_from_item_id(session, sample_items):
    item = sample_items[0]
    r = json.loads(tools.dispatch("add_block", {"block": {
        "type": "item", "headline": "h", "subheader": "s", "summary": "x",
        "item_id": item.id, "sources": ["https://model-typed-wrong-url.example"],
    }}, session))
    assert r["ok"]
    assert session.document[0]["sources"] == [{"url": item.url, "domain": "x"}]


def test_add_block_keeps_manual_sources_without_item_id(session, sample_items):
    r = json.loads(tools.dispatch("add_block", {"block": {
        "type": "item", "headline": "h", "subheader": "s", "summary": "x",
        "sources": ["https://multi-source-story.example/article"],
    }}, session))
    assert r["ok"]
    assert session.document[0]["sources"][0]["url"] == "https://multi-source-story.example/article"


def test_update_block_overrides_sources_from_item_id(session, sample_items):
    item = sample_items[1]
    r = json.loads(tools.dispatch("add_block", {"block": {
        "type": "item", "headline": "h", "subheader": "s", "summary": "x",
    }}, session))
    bid = r["block_id"]

    r = json.loads(tools.dispatch("update_block", {
        "block_id": bid,
        "fields": {"item_id": item.id, "sources": ["https://wrong.example"]},
    }, session))
    assert r["ok"]
    assert session.document[0]["sources"] == [{"url": item.url, "domain": "x"}]


def test_set_document_overrides_sources_from_item_id(session, sample_items):
    item = sample_items[0]
    r = json.loads(tools.dispatch("set_document", {"blocks": [
        {
            "type": "item", "headline": "h", "subheader": "s", "summary": "x",
            "item_id": item.id, "sources": ["https://wrong.example"],
        },
    ]}, session))
    assert r["block_count"] == 1
    assert session.document[0]["sources"] == [{"url": item.url, "domain": "x"}]


def test_add_block_unresolvable_item_id_keeps_manual_sources(session, sample_items):
    r = json.loads(tools.dispatch("add_block", {"block": {
        "type": "item", "headline": "h", "subheader": "s", "summary": "x",
        "item_id": 999999, "sources": ["https://fallback.example/article"],
    }}, session))
    assert r["ok"]
    assert session.document[0]["sources"][0]["url"] == "https://fallback.example/article"


def test_add_block_more_news_overrides_url_from_item_id(session, sample_items):
    item = sample_items[0]
    r = json.loads(tools.dispatch("add_block", {"block": {
        "type": "more_news",
        "items": [{"headline": "h", "url": "https://model-typed-wrong-url.example", "item_id": item.id}],
    }}, session))
    assert r["ok"]
    assert session.document[0]["items"][0]["url"] == item.url


def test_add_block_more_news_keeps_manual_url_without_item_id(session, sample_items):
    r = json.loads(tools.dispatch("add_block", {"block": {
        "type": "more_news",
        "items": [{"headline": "h", "url": "https://example.com/a-real-article"}],
    }}, session))
    assert r["ok"]
    assert session.document[0]["items"][0]["url"] == "https://example.com/a-real-article"


def test_data_tools_scope_and_item(session, sample_items):
    r = json.loads(tools.dispatch("list_scope_items", {}, session))
    assert r["count"] == len(sample_items)
    item_id = sample_items[0].id
    r = json.loads(tools.dispatch("get_item", {"item_id": item_id}, session))
    assert r["id"] == item_id
    assert "summary_text" in r
    assert "full_text" not in r  # dead field — NULL for nearly every item, dropped
    r = json.loads(tools.dispatch("get_item", {"item_id": 999999}, session))
    assert "error" in r


def test_add_block_rejects_non_dict_block_with_clean_error(session):
    # A JSON-encoded string instead of an object — observed in production to
    # crash with a raw AttributeError before this guard was added.
    r = json.loads(tools.dispatch("add_block", {
        "block": '{"type": "item", "headline": "h"}',
    }, session))
    assert "error" in r
    assert "AttributeError" not in r["error"]
    assert "must be a JSON object" in r["error"]
    assert session.document == []  # unchanged


def test_set_document_rejects_non_dict_blocks_with_clean_error(session):
    r = json.loads(tools.dispatch("set_document", {"blocks": ["not-a-block"]}, session))
    assert "error" in r
    assert "AttributeError" not in r["error"]
    assert "must be a JSON object" in r["error"]


def test_update_block_rejects_non_dict_fields_with_clean_error(session):
    r = json.loads(tools.dispatch("add_block", {"block": {"type": "divider"}}, session))
    bid = r["block_id"]
    r = json.loads(tools.dispatch("update_block", {"block_id": bid, "fields": "oops"}, session))
    assert "error" in r
    assert "must be a JSON object" in r["error"]


def test_get_document_defaults_compact_full_returns_complete(session):
    tools.dispatch("set_document", {"blocks": [
        {"type": "edition_header", "title": "Hi"},
        {"type": "divider"},
    ]}, session)

    compact = json.loads(tools.dispatch("get_document", {}, session))
    assert len(compact["blocks"]) == 2
    for b in compact["blocks"]:
        assert set(b.keys()) == {"id", "type"}

    full = json.loads(tools.dispatch("get_document", {"full": True}, session))
    assert full["blocks"][0]["title"] == "Hi"


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


def test_compose_system_prompt_includes_recent_quick_hits(session, agent_user, agent_summary):
    from app.models import utcnow
    memory.write_quick_hits(
        agent_user, agent_summary, utcnow().replace(tzinfo=None),
        [{"item_id": 7, "headline": "SOME-QUICK-HIT-HEADLINE"}],
    )
    sp = prompt.compose_system_prompt(agent_user, agent_summary)
    assert "RECENT QUICK HITS" in sp
    assert "SOME-QUICK-HIT-HEADLINE" in sp


def test_compose_system_prompt_self_heals_stale_content_config(db, agent_user, agent_summary):
    memory.write(
        agent_user, agent_summary, "content_config",
        "Merge duplicates into a `cluster`. Leftovers go in `quick_hits`.",
    )
    prompt.compose_system_prompt(agent_user, agent_summary)
    # The stored row itself was rewritten, not just the composed prompt —
    # so every future call (and every other consumer of this memory) sees
    # the corrected version too.
    stored = memory.read(agent_user, agent_summary, "content_config")
    assert "`cluster`" not in stored
    assert "`quick_hits`" not in stored
    assert "`item`" in stored
    assert "`more_news`" in stored


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


# ── Prompt caching + first-time nudge ────────────────────────────────────────

def test_opening_user_message_first_time_includes_nudge(session):
    msg = runner._opening_user_message(session, first_time=True)
    assert "exactly ONE set_document call" in msg
    assert "do not call get_document" in msg.lower()


def test_opening_user_message_revision_omits_nudge(session):
    msg = runner._opening_user_message(session, first_time=False)
    assert "exactly ONE set_document call" not in msg


def test_mark_cache_breakpoint_rolls_forward_and_clears_earlier():
    messages = [
        {"role": "system", "content": runner._cache_block("SYS")},
        {"role": "user", "content": "turn 1"},
    ]
    runner._mark_cache_breakpoint(messages)
    assert isinstance(messages[1]["content"], list)
    assert messages[1]["content"][0]["cache_control"] == {"type": "ephemeral"}

    messages.append({"role": "assistant", "content": "reply"})
    messages.append({"role": "tool", "content": "result"})
    runner._mark_cache_breakpoint(messages)
    # The earlier rolling breakpoint (on the old last message) is cleared
    # back to a plain string — only the system message and the new last
    # message carry cache_control, keeping us under Anthropic's 4-breakpoint cap.
    assert messages[1]["content"] == "turn 1"
    assert isinstance(messages[0]["content"], list)  # system breakpoint untouched
    assert isinstance(messages[-1]["content"], list)
    assert messages[-1]["content"][0]["text"] == "result"


def test_run_agent_marks_first_call_with_cache_control(monkeypatch, session):
    captured = []

    def fake_chat(messages, *, tools=None, api_key=None, model=None, **kw):
        captured.append([dict(m) for m in messages])
        return {"role": "assistant", "content": "done", "_usage": {}}

    monkeypatch.setattr("app.agent.runner.openrouter.chat", fake_chat)
    from app.agent.blocks import validate_document
    session.document = validate_document([{"type": "divider"}])
    runner.run_agent(session, api_key="sk", model="m")

    first_call_messages = captured[0]
    assert isinstance(first_call_messages[0]["content"], list)  # system, cached
    assert first_call_messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(first_call_messages[-1]["content"], list)  # rolling breakpoint
    assert "exactly ONE set_document call" in first_call_messages[-1]["content"][0]["text"]
