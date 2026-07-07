"""LLM-based tagging with structured output.

Given an item's text and the candidate tags (name, keywords, explanation), ask
the model which tags apply, with a confidence per tag.
"""
from __future__ import annotations

import logging

from ..llm import openrouter
from ..llm.prompt_safety import ANTI_INJECTION_NOTE, wrap_untrusted

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a precise news classifier. Given a news item and a taxonomy of "
    "tags (each with a name, keywords and explanation), decide which tags apply "
    "to the item. Only assign a tag when the item genuinely matches its meaning. "
    "Return a confidence in [0,1] for each assigned tag.\n\n"
    'Respond ONLY with valid JSON in this exact format: {"tags": [{"name": "TagName", "confidence": 0.9}, ...]}'
    "\n\n" + ANTI_INJECTION_NOTE
)


def _schema(tag_names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": tag_names},
                        "confidence": {"type": "number"},
                    },
                    "required": ["name", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tags"],
        "additionalProperties": False,
    }


def score_item(
    item_text: str,
    tags: list[dict],
    *,
    api_key: str | None = None,
    model: str | None = None,
    usage_hook=None,
) -> dict[str, float]:
    """tags: [{name, keywords, explanation}]. Returns {tag_name: confidence}.

    ``api_key``/``model`` let a source's assigned ApiKey drive tagging of its
    own items instead of the global OPENROUTER_API_KEY.
    """
    if not tags or not (api_key or openrouter.is_configured()):
        return {}

    taxonomy_lines = []
    for t in tags:
        kw = ", ".join(t.get("keywords") or [])
        taxonomy_lines.append(
            f"- {t['name']}: keywords=[{kw}]; meaning: {t.get('explanation') or ''}"
        )
    taxonomy = "\n".join(taxonomy_lines)

    user = (
        f"Taxonomy:\n{taxonomy}\n\n"
        f"News item:\n{wrap_untrusted(item_text[:8000])}\n\n"
        "Return the tags that apply with confidences."
    )
    names = [t["name"] for t in tags]
    try:
        result = openrouter.chat_json(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            schema=_schema(names),
            api_key=api_key,
            model=model,
            usage_hook=usage_hook,
        )
    except openrouter.LLMError as exc:
        logger.error("LLM tagging failed: %s", exc)
        return {}

    out: dict[str, float] = {}
    for entry in (result or {}).get("tags", []):
        name = entry.get("name")
        if name in names:
            out[name] = float(entry.get("confidence", 0.0))
    return out
