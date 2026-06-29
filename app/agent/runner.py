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


def _items_digest(session: AgentSession) -> str:
    """A compact list of in-scope items for the opening user message."""
    lines = []
    for it in session.items:
        src = it.source.name if it.source else "?"
        when = (it.published_at or it.fetched_at)
        when_s = when.strftime("%Y-%m-%d") if when else "?"
        lines.append(
            f"#{it.id} [{it.item_type or 'other'}] {it.title} "
            f"— {it.one_liner or ''} ({src}, {when_s})"
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
) -> list[dict]:
    """Run the agent loop and return the final block document.

    ``seed_document`` pre-loads the draft (used for feedback revisions so the
    agent edits the existing edition rather than starting from scratch).
    """
    if seed_document is not None:
        session.document = list(seed_document)

    max_steps = current_app.config.get("AGENT_MAX_STEPS", 24)
    max_tokens = current_app.config.get("AGENT_MAX_TOKENS", 0)

    messages = [
        {"role": "system", "content": compose_system_prompt(session.user, session.summary)},
        {"role": "user", "content": _opening_user_message(session, extra_instruction)},
    ]

    for step in range(max_steps):
        message = openrouter.chat(
            messages, tools=tools.TOOL_SPECS, api_key=api_key, model=model
        )
        usage = message.get("_usage") or {}
        session.tokens_used += int(usage.get("total_tokens") or 0)

        messages.append(_clean_assistant_message(message))
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            # No more tool calls → the agent considers the edition done.
            break

        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = tools.dispatch(name, args, session)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": result,
            })

        if max_tokens and session.tokens_used >= max_tokens:
            logger.warning(
                "Agent token budget reached (%d) for summary %d; stopping.",
                session.tokens_used, session.summary.id,
            )
            break
    else:
        logger.warning(
            "Agent hit max steps (%d) for summary %d.", max_steps, session.summary.id
        )

    if not session.document:
        raise AgentError("Agent produced an empty document.")

    return session.document
