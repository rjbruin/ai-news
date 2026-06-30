"""The agent loop: drive an OpenRouter tool-calling model to build an edition."""
from __future__ import annotations

import json
import logging

from flask import current_app

from ..llm import openrouter
from . import tools
from .context import AgentSession
from .prompt import compose_system_prompt

logger = logging.getLogger(__name__)


class AgentError(RuntimeError):
    """Raised when an agent run cannot complete."""


class AgentCancelled(RuntimeError):
    """Raised when a run is cancelled mid-flight via its cancel_event."""


def _items_digest(session: AgentSession) -> str:
    """A compact list of in-scope items for the opening user message.

    Deliberately omits the ingestion source/feed name — it's the same value
    for nearly every item (the mailbox it was fetched from) and was getting
    picked up by the model as if it were a per-article citation. Use
    get_item/list_scope_items' url_domain for real attribution instead.
    """
    lines = []
    for it in session.items:
        when = (it.published_at or it.fetched_at)
        when_s = when.strftime("%Y-%m-%d") if when else "?"
        lines.append(
            f"#{it.id} [{it.item_type or 'other'}] {it.title} "
            f"— {it.one_liner or ''} ({when_s})"
        )
    return "\n".join(lines) if lines else "(no items in scope)"


def _opening_user_message(session: AgentSession, extra: str | None = None) -> str:
    rng = ""
    if session.range_start or session.range_end:
        rng = (
            f"Scope window: "
            f"{session.range_start.isoformat() if session.range_start else 'beginning'} "
            f"→ {session.range_end.isoformat() if session.range_end else 'now'}\n\n"
        )
    msg = (
        f"Create the next edition of '{session.summary.name}'.\n\n"
        f"{rng}"
        f"{len(session.items)} item(s) in scope:\n{_items_digest(session)}\n\n"
        f"Use get_item for full text. Build the document with your editor tools, "
        f"then call write_headlines. Follow the content configuration and interests."
    )
    if extra:
        msg += f"\n\n{extra}"
    return msg


def _clean_assistant_message(msg: dict) -> dict:
    """Strip non-protocol keys before echoing the assistant turn back."""
    out = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        out["tool_calls"] = msg["tool_calls"]
    return out


def run_agent(
    session: AgentSession,
    *,
    api_key: str,
    model: str,
    extra_instruction: str | None = None,
    seed_document: list[dict] | None = None,
    log_fn=None,
    cancel_event=None,
    max_steps: int | None = None,
) -> list[dict]:
    """Run the agent loop and return the final block document.

    ``seed_document`` pre-loads the draft (used for feedback revisions so the
    agent edits the existing edition rather than starting from scratch).
    ``log_fn`` receives event dicts as the run progresses (used for live UI).
    ``cancel_event`` (a threading.Event), if set, is checked between steps;
    when set, the run stops and raises AgentCancelled rather than persisting
    a partial document. Cancellation is cooperative — it takes effect after
    the in-flight LLM call for the current step finishes.
    ``max_steps``, if given, overrides the AGENT_MAX_STEPS config default —
    used for the per-summary "Max agent steps" setting.
    """
    if seed_document is not None:
        session.document = list(seed_document)

    if max_steps is None:
        max_steps = current_app.config.get("AGENT_MAX_STEPS", 50)
    max_tokens = current_app.config.get("AGENT_MAX_TOKENS", 0)

    def emit(event: dict) -> None:
        if log_fn:
            log_fn(event)

    emit({"type": "start", "items": len(session.items), "model": model})

    messages = [
        {"role": "system", "content": compose_system_prompt(session.user, session.summary)},
        {"role": "user", "content": _opening_user_message(session, extra_instruction)},
    ]

    for step in range(max_steps):
        if cancel_event is not None and cancel_event.is_set():
            emit({"type": "cancelled", "step": step + 1})
            raise AgentCancelled("Generation cancelled.")

        emit({"type": "llm_call", "step": step + 1})
        message = openrouter.chat(
            messages, tools=tools.TOOL_SPECS, api_key=api_key, model=model
        )
        usage = message.get("_usage") or {}
        step_tokens = int(usage.get("total_tokens") or 0)
        step_cost = float(usage.get("cost") or 0.0)
        session.tokens_used += step_tokens
        session.cost_used += step_cost
        emit({"type": "usage", "step": step + 1, "tokens": step_tokens,
              "total_tokens": session.tokens_used, "cost": step_cost,
              "total_cost": session.cost_used})

        messages.append(_clean_assistant_message(message))
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            emit({"type": "stop", "reason": "no_tool_calls", "step": step + 1,
                  "total_tokens": session.tokens_used, "total_cost": session.cost_used})
            break

        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}

            args_parts = [f"{k}={repr(v)[:60]}" for k, v in list(args.items())[:6]]
            emit({"type": "tool_call", "step": step + 1, "name": name,
                  "args_preview": ", ".join(args_parts) or "(no args)"})

            result = tools.dispatch(name, args, session)

            emit({"type": "tool_result", "step": step + 1, "name": name,
                  "preview": result[:300] if len(result) > 300 else result})

            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": result,
            })

        if max_tokens and session.tokens_used >= max_tokens:
            emit({"type": "stop", "reason": "token_limit", "step": step + 1,
                  "total_tokens": session.tokens_used, "total_cost": session.cost_used})
            logger.warning(
                "Agent token budget reached (%d) for summary %d; stopping.",
                session.tokens_used, session.summary.id,
            )
            break
    else:
        emit({"type": "stop", "reason": "max_steps", "step": max_steps,
              "total_tokens": session.tokens_used, "total_cost": session.cost_used})
        logger.warning(
            "Agent hit max steps (%d) for summary %d.", max_steps, session.summary.id
        )

    if not session.document:
        raise AgentError("Agent produced an empty document.")

    return session.document
