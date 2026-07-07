"""Resolve the OpenRouter credentials an agent run should use.

The agentic pipeline uses whichever of the user's own ApiKey rows they've
selected "for editions" on the API Keys page. The global/shared key is
intentionally NOT selectable for this — agentic runs are billed to the user,
so a missing key is a clear, actionable error rather than a silent charge to
the shared account.
"""
from __future__ import annotations


class MissingCredentials(RuntimeError):
    """Raised when a user has not selected an API key for editions."""


def resolve(user) -> tuple[str, str]:
    """Return (api_key, model) for an agent run, or raise MissingCredentials."""
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
    return secret, key.resolved_model()
