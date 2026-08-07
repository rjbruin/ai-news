"""Tests for Ask Dispatch.

The interesting surface is citation *provenance*: no URL in an answer may be
model-authored, every rendered link must resolve to something really in the
archive, and a citation that doesn't resolve must be visible rather than
silently dropped.
"""
import json

import pytest

from app.models import ApiKey, NewsItem, Summary, SummaryRun
from app.services import ask as ask_svc


def _dispatch(db, user, name="D"):
    s = Summary(user_id=user.id, name=name, type_key="agentic_page", params={})
    db.session.add(s)
    db.session.commit()
    return s


def _item(db, title, url="https://example.com/a", n=[0]):
    n[0] += 1
    it = NewsItem(dedup_hash=f"ask{n[0]}", title=title, url=url,
                  summary_text="Body text about the thing.", one_liner="A thing happened.")
    db.session.add(it)
    db.session.commit()
    return it


def _run(db, dispatch, blocks=None, label="Mon"):
    run = SummaryRun(summary_id=dispatch.id, label=label, status="ok",
                     content="<p>x</p>",
                     document=blocks if blocks is not None else [
                         {"type": "item", "id": "blk1", "headline": "A headline",
                          "subheader": "s", "summary": "The story text.",
                          "item_id": None, "sources": []}])
    db.session.add(run)
    db.session.commit()
    return run


def _give_key(db, user, secret="sk-or-ask"):
    key = ApiKey(owner_user_id=user.id, label="OpenRouter key", provider="openrouter")
    key.set_key(secret)
    db.session.add(key)
    db.session.commit()
    return key


# ── Gating ──────────────────────────────────────────────────────────────────

def test_ask_page_prompts_for_key_when_missing(auth_client, db, user):
    d = _dispatch(db, user)
    html = auth_client.get(f"/summaries/{d.id}/ask").data.decode()
    assert "Add an API key" in html
    assert "<textarea" not in html


def test_ask_page_available_with_key(auth_client, db, user):
    d = _dispatch(db, user)
    _give_key(db, user)
    html = auth_client.get(f"/summaries/{d.id}/ask").data.decode()
    assert "<textarea" in html
    assert "Add an API key" not in html


def test_ask_requires_read_access(auth_client, db, user, admin):
    other = _dispatch(db, admin, name="Theirs")
    _give_key(db, user)
    assert auth_client.get(f"/summaries/{other.id}/ask").status_code == 403


def test_follower_may_ask(auth_client, db, user, admin):
    other = _dispatch(db, admin, name="Theirs")
    user.follow(other)
    _give_key(db, user)
    db.session.commit()
    assert auth_client.get(f"/summaries/{other.id}/ask").status_code == 200


# ── Discoverability ─────────────────────────────────────────────────────────
# The feature shipped reachable only from the per-Dispatch detail page, which
# is a click deeper than anyone looks. These pin the entry points.

def test_dispatches_directory_links_to_ask_for_own(auth_client, db, user):
    d = _dispatch(db, user)
    user.follow(d)
    db.session.commit()
    html = auth_client.get("/dispatches").data.decode()
    assert f"/summaries/{d.id}/ask" in html


def test_dispatches_directory_links_to_ask_for_followed(auth_client, db, user, admin):
    other = _dispatch(db, admin, name="Theirs")
    other.is_published = True
    other.published_name = "Theirs"
    user.follow(other)
    db.session.commit()
    html = auth_client.get("/dispatches").data.decode()
    assert f"/summaries/{other.id}/ask" in html


def test_dispatches_directory_hides_ask_for_unfollowed(auth_client, db, user, admin):
    """No point offering to answer questions about a Dispatch you don't read —
    and the route would 403 anyway."""
    other = _dispatch(db, admin, name="Theirs")
    other.is_published = True
    other.published_name = "Theirs"
    db.session.commit()
    html = auth_client.get("/dispatches").data.decode()
    assert f"/summaries/{other.id}/ask" not in html


def test_dispatch_detail_links_to_ask(auth_client, db, user):
    d = _dispatch(db, user)
    html = auth_client.get(f"/dispatches/{d.id}").data.decode()
    assert f"/summaries/{d.id}/ask" in html


# ── Citation rendering ──────────────────────────────────────────────────────

def test_item_citation_becomes_a_link(app, db, user):
    d = _dispatch(db, user)
    item = _item(db, "Model X released", url="https://news.example/model-x")
    with app.test_request_context():
        html, refs = ask_svc.render_answer(f"Model X shipped [item:{item.id}].", d)
    assert "https://news.example/model-x" in html
    assert refs[0]["label"] == "Model X released"
    assert refs[0]["kind"] == "item"


def test_edition_block_citation_links_to_anchor(app, db, user):
    d = _dispatch(db, user)
    run = _run(db, d)
    with app.test_request_context():
        html, refs = ask_svc.render_answer(f"As covered [run:{run.id}#blk1].", d)
    assert f"/summaries/{d.id}/editions/{run.id}#blk1" in html
    assert "A headline" in refs[0]["label"]


def test_unresolvable_citation_is_shown_not_dropped(app, db, user):
    """A fabricated id must be visible to the reader, not silently removed —
    otherwise a hallucinated citation reads as an uncited claim."""
    d = _dispatch(db, user)
    with app.test_request_context():
        html, refs = ask_svc.render_answer("This is false [item:999999].", d)
    assert "unverified reference" in html
    assert refs == []


def test_citation_to_another_dispatchs_edition_does_not_resolve(app, db, user, admin):
    """Ask Dispatch is scoped to one Dispatch; a run id from elsewhere must not
    become a working link into someone else's editions."""
    mine = _dispatch(db, user, name="Mine")
    theirs = _dispatch(db, admin, name="Theirs")
    their_run = _run(db, theirs)
    with app.test_request_context():
        html, refs = ask_svc.render_answer(f"Leak [run:{their_run.id}].", mine)
    assert "unverified reference" in html
    assert refs == []


def test_model_authored_html_is_escaped(app, db, user):
    """Nothing the model writes may reach the page as markup — in particular
    it must not be able to author its own <a href>."""
    d = _dispatch(db, user)
    evil = 'See <a href="https://evil.test">here</a> and <script>alert(1)</script>'
    with app.test_request_context():
        html, refs = ask_svc.render_answer(evil, d)
    assert "<a href=\"https://evil.test\"" not in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert refs == []


def test_bare_url_does_not_become_a_link(app, db, user):
    """A URL the model types is not a citation and gets no anchor tag."""
    d = _dispatch(db, user)
    with app.test_request_context():
        html, _ = ask_svc.render_answer("Source: https://made-up.example/story", d)
    assert "<a " not in html


def test_repeated_citation_reuses_one_reference_number(app, db, user):
    d = _dispatch(db, user)
    item = _item(db, "Repeated item")
    with app.test_request_context():
        html, refs = ask_svc.render_answer(
            f"One [item:{item.id}]. Two [item:{item.id}].", d)
    assert len(refs) == 1
    assert html.count("[1]") == 2


# ── Tools ───────────────────────────────────────────────────────────────────

def test_search_news_returns_citable_ids(app, db, user):
    d = _dispatch(db, user)
    item = _item(db, "Quantum breakthrough announced")
    with app.test_request_context():
        out = json.loads(ask_svc._run_tool("search_news", {"query": "quantum"}, d))
    assert out["count"] == 1
    assert out["items"][0]["item_id"] == item.id
    assert out["items"][0]["cite_as"] == f"[item:{item.id}]"


def test_get_edition_refuses_another_dispatch(app, db, user, admin):
    """Reading editions is scoped to the asked Dispatch, so Ask Dispatch can't
    be used to read another user's unpublished editions."""
    mine = _dispatch(db, user, name="Mine")
    theirs = _dispatch(db, admin, name="Theirs")
    their_run = _run(db, theirs)
    with app.test_request_context():
        out = json.loads(ask_svc._run_tool("get_edition", {"run_id": their_run.id}, mine))
    assert "error" in out


def test_search_editions_finds_matching_block(app, db, user):
    d = _dispatch(db, user)
    run = _run(db, d, blocks=[{"type": "item", "id": "bx", "headline": "Fusion milestone",
                               "subheader": "s", "summary": "Net energy gain achieved.",
                               "item_id": None, "sources": []}])
    with app.test_request_context():
        out = json.loads(ask_svc._run_tool("search_editions", {"query": "fusion"}, d))
    assert out["count"] == 1
    assert out["editions"][0]["run_id"] == run.id
    assert out["editions"][0]["matching_blocks"][0]["cite_as"] == f"[run:{run.id}#bx]"


def test_search_limit_is_clamped(app, db, user):
    d = _dispatch(db, user)
    for i in range(40):
        _item(db, f"Story about widgets {i}")
    with app.test_request_context():
        out = json.loads(ask_svc._run_tool("search_news", {"query": "widgets", "limit": 999}, d))
    assert out["count"] <= ask_svc.MAX_SEARCH_RESULTS


# ── The loop ────────────────────────────────────────────────────────────────

def test_ask_runs_tools_then_answers(app, db, user, monkeypatch):
    d = _dispatch(db, user)
    item = _item(db, "Widget prices fell")
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": "", "tool_calls": [{
                "id": "c1",
                "function": {"name": "search_news",
                             "arguments": json.dumps({"query": "widget"})},
            }], "_usage": {"total_tokens": 10, "cost": 0.001}}
        return {"content": f"Prices fell [item:{item.id}].",
                "_usage": {"total_tokens": 5, "cost": 0.002}}

    monkeypatch.setattr("app.llm.openrouter.chat", fake_chat)
    with app.test_request_context():
        result = ask_svc.ask(d, "What happened to widgets?", api_key="k", model="m")

    assert len(calls) == 2
    assert result["cost"] == pytest.approx(0.003)
    assert result["tokens"] == 15
    assert len(result["references"]) == 1
    assert "Widget prices fell" in result["references"][0]["label"]
