"""Tests for typed application settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from rewire.core.config import LogFormat, LogLevel, Settings, get_settings


def test_defaults_are_safe(settings: Settings) -> None:
    assert settings.environment == "local"
    assert settings.debug is False
    assert settings.log_level is LogLevel.INFO
    assert settings.log_format is LogFormat.CONSOLE
    # No LLM provider by default: deterministic analysis must not require one.
    assert settings.llm.provider == "null"
    # The sandbox must default to the most restrictive policy.
    assert settings.sandbox.network == "none"
    assert settings.sandbox.read_only_rootfs is True


def test_settings_are_frozen(settings: Settings) -> None:
    with pytest.raises(ValidationError):
        settings.debug = True  # type: ignore[misc]


def test_env_vars_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWIRE_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("REWIRE_ENVIRONMENT", "ci")
    loaded = Settings(_env_file=None)
    assert loaded.log_level is LogLevel.DEBUG
    assert loaded.environment == "ci"


def test_nested_env_vars_use_double_underscore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWIRE_SANDBOX__MEMORY_LIMIT_MB", "4096")
    monkeypatch.setenv("REWIRE_AGENT__MAX_ITERATIONS", "5")
    loaded = Settings(_env_file=None)
    assert loaded.sandbox.memory_limit_mb == 4096
    assert loaded.agent.max_iterations == 5


def test_invalid_values_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWIRE_AGENT__MAX_ITERATIONS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_unknown_env_vars_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWIRE_TOTALLY_MADE_UP", "1")
    assert Settings(_env_file=None).environment == "local"


def test_secrets_are_not_exposed_by_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWIRE_LLM__ANTHROPIC_API_KEY", "sk-ant-supersecret")
    loaded = Settings(_env_file=None)

    assert loaded.llm.anthropic_api_key is not None
    assert loaded.llm.anthropic_api_key.get_secret_value() == "sk-ant-supersecret"
    assert "supersecret" not in repr(loaded)
    assert "supersecret" not in str(loaded.model_dump(mode="json"))


def test_derived_directories_hang_off_data_dir(tmp_path: Path) -> None:
    loaded = Settings(data_dir=tmp_path / "artifacts", _env_file=None)
    assert loaded.index_dir == tmp_path / "artifacts" / "index"
    assert loaded.runs_dir == tmp_path / "artifacts" / "runs"


def test_ensure_data_dirs_is_idempotent(settings: Settings) -> None:
    settings.ensure_data_dirs()
    settings.ensure_data_dirs()
    assert settings.data_dir.is_dir()
    assert settings.index_dir.is_dir()
    assert settings.runs_dir.is_dir()


def test_data_dir_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWIRE_DATA_DIR", "~/rewire-data")
    assert "~" not in str(Settings(_env_file=None).data_dir)


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
