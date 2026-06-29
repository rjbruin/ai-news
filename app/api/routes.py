"""JSON API routes (login-required, scoped to the current user)."""
from __future__ import annotations

from flask import Blueprint, abort, jsonify, request
from flask_login import current_user, login_required

from ..agent import memory as agent_memory
from ..extensions import db
from ..models import NewsItem, Summary, SummaryRun
from ..services import summarize

bp = Blueprint("api", __name__, url_prefix="/api")

_WRITABLE_KINDS = ("interests", "content_config", "history")


# ── helpers ─────────────────────────────────────────────────────────────────

def _own_summary(summary_id: int) -> Summary:
    summary = db.session.get(Summary, summary_id)
    if summary is None:
        abort(404)
    if summary.user_id != current_user.id:
        abort(403)
    return summary


def _own_run(summary: Summary, run_id: int) -> SummaryRun:
    run = db.session.get(SummaryRun, run_id)
    if run is None or run.summary_id != summary.id:
        abort(404)
    return run


def _item_json(item: NewsItem, *, full: bool = False) -> dict:
    d = {
        "id": item.id,
        "title": item.title,
        "one_liner": item.one_liner,
        "item_type": item.item_type,
        "source": item.source.name if item.source else None,
        "url": item.url,
        "published_at": (item.published_at or item.fetched_at).isoformat()
        if (item.published_at or item.fetched_at) else None,
    }
    if full:
        d["summary_text"] = item.summary_text
        d["full_text"] = item.full_text
    return d


def _run_json(run: SummaryRun, *, with_document: bool = False) -> dict:
    d = {
        "run_id": run.id,
        "label": run.label,
        "generated_at": run.generated_at.isoformat() if run.generated_at else None,
        "item_count": run.item_count,
        "revision": run.revision,
        "parent_run_id": run.parent_run_id,
        "status": run.status,
    }
    if with_document:
        d["document"] = run.document or []
        d["content"] = run.content
    return d


# ── items ────────────────────────────────────────────────────────────────────

@bp.get("/summaries/<int:summary_id>/scope-items")
@login_required
def scope_items(summary_id: int):
    summary = _own_summary(summary_id)
    items = summarize.items_in_scope(summary)
    return jsonify({"count": len(items), "items": [_item_json(i) for i in items]})


@bp.get("/items/<int:item_id>")
@login_required
def get_item(item_id: int):
    item = db.session.get(NewsItem, item_id)
    if item is None:
        abort(404)
    return jsonify(_item_json(item, full=True))


# ── editions ──────────────────────────────────────────────────────────────────

@bp.get("/summaries/<int:summary_id>/editions")
@login_required
def list_editions(summary_id: int):
    summary = _own_summary(summary_id)
    heads = summarize.edition_heads(summary)
    return jsonify({"editions": [_run_json(r) for r in heads]})


@bp.get("/summaries/<int:summary_id>/editions/<int:run_id>")
@login_required
def get_edition(summary_id: int, run_id: int):
    summary = _own_summary(summary_id)
    run = _own_run(summary, run_id)
    data = _run_json(run, with_document=True)
    data["revisions"] = [_run_json(r) for r in summarize.revision_chain(run)]
    return jsonify(data)


@bp.post("/summaries/<int:summary_id>/editions/<int:run_id>/feedback")
@login_required
def edition_feedback(summary_id: int, run_id: int):
    summary = _own_summary(summary_id)
    run = _own_run(summary, run_id)
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Feedback text is required."}), 400

    from ..agent.creds import MissingCredentials
    try:
        new_run = summarize.revise_edition(run, text)
    except MissingCredentials as exc:
        return jsonify({"error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_run_json(new_run, with_document=True)), 201


# ── memory ────────────────────────────────────────────────────────────────────

@bp.get("/summaries/<int:summary_id>/memory/<kind>")
@login_required
def read_memory(summary_id: int, kind: str):
    summary = _own_summary(summary_id)
    if kind not in _WRITABLE_KINDS:
        abort(404)
    return jsonify({"kind": kind, "content": agent_memory.read(summary.user, summary, kind)})


@bp.put("/summaries/<int:summary_id>/memory/<kind>")
@login_required
def write_memory(summary_id: int, kind: str):
    summary = _own_summary(summary_id)
    if kind not in _WRITABLE_KINDS:
        abort(404)
    payload = request.get_json(silent=True) or {}
    content = payload.get("content", "")
    agent_memory.write(summary.user, summary, kind, content)
    return jsonify({"ok": True, "kind": kind})


@bp.get("/summaries/<int:summary_id>/headlines")
@login_required
def list_headlines(summary_id: int):
    summary = _own_summary(summary_id)
    days = request.args.get("days", default=30, type=int)
    rows = agent_memory.recent_headlines(summary.user, summary, days=days)
    return jsonify({"headlines": [
        {"edition_ts": r.edition_ts.isoformat() if r.edition_ts else None,
         "content": r.content}
        for r in rows
    ]})
