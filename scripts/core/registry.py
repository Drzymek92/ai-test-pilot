"""name → adapter resolution. The ONLY place core learns an adapter exists.

Adapters never import each other; core never imports an adapter module except here.
That seam is the "reusable flows, no duplication" demonstration.
"""
from __future__ import annotations

from importlib import import_module
from types import ModuleType

# Registered adapters: logical name → module path. Add one line per new adapter.
_ADAPTERS: dict[str, str] = {
    "python_pytest": "scripts.adapters.python_pytest",
    "web_playwright": "scripts.adapters.web_playwright",
}


def get_adapter(name: str) -> ModuleType:
    try:
        module_path = _ADAPTERS[name]
    except KeyError:
        known = ", ".join(sorted(_ADAPTERS))
        raise ValueError(f"Unknown adapter '{name}'. Known adapters: {known}.")
    return import_module(module_path)


def available() -> list[str]:
    return sorted(_ADAPTERS)
