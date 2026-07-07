"""Discovery + registry for news source plugins.

Any module under ``app/sources/`` that defines a ``NewsSource`` subclass with a
non-empty ``type_key`` is auto-registered. Drop in a file → new source type.
"""
from __future__ import annotations

import importlib
import pkgutil

from .base import NewsSource

_REGISTRY: dict[str, type[NewsSource]] = {}


def register(cls: type[NewsSource]) -> type[NewsSource]:
    if cls.type_key:
        _REGISTRY[cls.type_key] = cls
    return cls


def discover() -> None:
    """Import all submodules so their NewsSource subclasses register."""
    import app.sources as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name in {"base", "registry", "extract"}:
            continue
        module = importlib.import_module(f"app.sources.{mod.name}")
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, NewsSource)
                and attr is not NewsSource
                and attr.type_key
            ):
                register(attr)


def get(type_key: str) -> type[NewsSource] | None:
    return _REGISTRY.get(type_key)


def create(
    type_key: str,
    config: dict | None = None,
    *,
    api_key: str | None = None,
    model: str | None = None,
    usage_hook=None,
) -> NewsSource | None:
    cls = get(type_key)
    if cls is None:
        return None
    return cls(config, api_key=api_key, model=model, usage_hook=usage_hook)


def all_types() -> dict[str, type[NewsSource]]:
    return dict(_REGISTRY)
