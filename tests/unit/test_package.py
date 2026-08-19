"""Smoke tests for package layout and importability."""

from __future__ import annotations

import importlib
import pkgutil

import rewire

SUBPACKAGES = [
    "agents",
    "analyzers",
    "api",
    "changes",
    "core",
    "evals",
    "gitio",
    "llm",
    "models",
    "sandbox",
    "services",
]


def test_version_is_exposed() -> None:
    assert rewire.__version__
    assert rewire.__version__ != "0.0.0+unknown", "package should be installed, not run from source"


def test_every_declared_subpackage_exists() -> None:
    found = {module.name for module in pkgutil.iter_modules(rewire.__path__) if module.ispkg}
    assert set(SUBPACKAGES) <= found


def test_every_module_imports_cleanly() -> None:
    for name in [*SUBPACKAGES, "cli"]:
        assert importlib.import_module(f"rewire.{name}") is not None
