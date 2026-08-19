"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from rewire.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Keep the settings singleton from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip REWIRE_* variables so the developer's shell cannot alter test results."""
    import os

    for key in list(os.environ):
        if key.startswith("REWIRE_"):
            monkeypatch.delenv(key, raising=False)


SPECS_DIR = Path(__file__).parent / "fixtures" / "specs"


@pytest.fixture
def specs() -> Path:
    """Directory holding the OpenAPI fixture specifications."""
    return SPECS_DIR


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at an isolated temporary data directory."""
    return Settings(data_dir=tmp_path / ".rewire", _env_file=None)
