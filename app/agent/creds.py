"""Resolve the OpenRouter credentials an agent run should use.

The agentic pipeline uses whichever of the user's own ApiKey rows they've
selected "for editions" on the API Keys page for the *secret* — agentic runs
are billed to the user, so a missing key is a clear, actionable error rather
than a silent charge to the shared account. The *model*, however, is an
independent per-edition choice (see the summary's "model" setting), not tied
to which key pays for it.
"""
from __future__ import annotations


class MissingCredentials(RuntimeError):
    """Raised when a user has not selected an API key for editions."""


def resolve(user, summary=None) -> tuple[str, str]:
    """Return (api_key, model) for an agent run, or raise MissingCredentials.

    ``model`` comes from ``summary.params["model"]`` when set (see the
    summary's "Model" setting), falling back to the system default.
    """
    from flask import current_app

    key = user.edition_api_key
    if key is None or not key.active:
        raise MissingCredentials(
            "No API key selected for editions. Pick one on the API Keys page."
        )
    secret = key.get_key()
    if not secret:
        raise MissingCredentials(
            "The API key selected for editions has no usable credential."
        )
    model = None
    if summary is not None:
        model = (summary.params or {}).get("model") or None
    model = model or current_app.config.get("OPENROUTER_MODEL")
    return secret, model
