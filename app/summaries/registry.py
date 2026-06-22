"""Discovery + registry for summary plugins (mirrors sources/registry.py)."""
from __future__ import annotations

import importlib
import pkgutil

from .base import NewsSummary

_REGISTRY: dict[str, type[NewsSummary]] = {}


def register(cls: type[NewsSummary]) -> type[NewsSummary]:
    if cls.type_key:
        _REGISTRY[cls.type_key] = cls
    return cls


def discover() -> None:
    import app.summaries as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name in {"base", "registry"}:
            continue
        module = importlib.import_module(f"app.summaries.{mod.name}")
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, NewsSummary)
                and attr is not NewsSummary
                and attr.type_key
            ):
                register(attr)


def get(type_key: str) -> type[NewsSummary] | None:
    return _REGISTRY.get(type_key)


def create(type_key: str) -> NewsSummary | None:
    cls = get(type_key)
    return cls() if cls else None


def all_types() -> dict[str, type[NewsSummary]]:
    return dict(_REGISTRY)
