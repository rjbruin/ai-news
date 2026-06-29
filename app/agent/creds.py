"""Resolve the OpenRouter credentials an agent run should use.

The agentic pipeline uses the user's own key + model. The global
OPENROUTER_API_KEY is intentionally NOT used as a fallback here — agentic runs
are billed to the user, so a missing key is a clear, actionable error rather
than a silent charge to the global account.
"""
from __future__ import annotations

from flask import current_app


class MissingCredentials(RuntimeError):
    """Raised when a user has not configured an OpenRouter key for the agent."""


def resolve(user) -> tuple[str, str]:
    """Return (api_key, model) for an agent run, or raise MissingCredentials."""
    key = user.get_openrouter_key()
    if not key:
        raise MissingCredentials(
            "No OpenRouter API key configured. Add one in Settings to use "
            "agentic summaries."
        )
    model = user.openrouter_model or current_app.config.get(
        "OPENROUTER_MODEL", "anthropic/claude-haiku-4.5"
    )
    return key, model
