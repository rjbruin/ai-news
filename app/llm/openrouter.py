"""Thin OpenRouter client with JSON / structured-output helpers.

Uses the global ``OPENROUTER_API_KEY`` (all analysis is global per project decision).
"""
from __future__ import annotations

import json
import logging

import httpx
from flask import current_app

logger = logging.getLogger(__name__)


class LLMNotConfigured(RuntimeError):
    """Raised when no OpenRouter key is configured."""


class LLMError(RuntimeError):
    """Raised on an API or parsing failure."""


def is_configured() -> bool:
    return bool(current_app.config.get("OPENROUTER_API_KEY"))


def chat_json(
    messages: list[dict],
    *,
    schema: dict | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    timeout: float = 60.0,
) -> dict | list:
    """Call OpenRouter chat completions and return parsed JSON.

    Always uses ``json_object`` response format (universally supported on
    OpenRouter).  When ``schema`` is provided its structure is described in the
    system prompt so the model follows it — we don't use ``json_schema`` mode
    because it is not supported by all providers on OpenRouter.
    """
    api_key = current_app.config.get("OPENROUTER_API_KEY")
    if not api_key:
        raise LLMNotConfigured("OPENROUTER_API_KEY is not set")

    base_url = current_app.config["OPENROUTER_BASE_URL"].rstrip("/")
    model = model or current_app.config["OPENROUTER_MODEL"]

    if schema is not None:
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "result", "strict": True, "schema": schema},
        }
    else:
        response_format = {"type": "json_object"}

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": response_format,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "AI News",
    }
    pub = current_app.config.get("PUBLIC_URL")
    if pub:
        headers["HTTP-Referer"] = pub

    try:
        resp = httpx.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = ""
        try:
            body = exc.response.json()
        except Exception:
            body = exc.response.text[:500]
        raise LLMError(
            f"OpenRouter HTTP {exc.response.status_code}: {body}"
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"OpenRouter request failed: {exc}") from exc

    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"Unexpected OpenRouter response: {data}") from exc

    # Strip markdown code fences if the model wrapped the JSON
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # drop first line (```json or ```) and last line (```)
        stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM did not return valid JSON: {content[:500]}") from exc
