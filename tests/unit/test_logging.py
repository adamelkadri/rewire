"""Tests for structured logging, especially secret redaction."""

from __future__ import annotations

import json

import pytest
import structlog

from rewire.core.config import LogFormat, LogLevel, Settings
from rewire.core.logging import (
    REDACTED_PLACEHOLDER,
    configure_from_settings,
    configure_logging,
    get_logger,
    redact_secrets,
)


def test_redacts_top_level_secret_keys() -> None:
    event = redact_secrets(None, "info", {"event": "call", "api_key": "sk-live-123"})
    assert event["api_key"] == REDACTED_PLACEHOLDER


def test_redaction_is_case_insensitive() -> None:
    event = redact_secrets(None, "info", {"event": "call", "Authorization": "Bearer abc"})
    assert event["Authorization"] == REDACTED_PLACEHOLDER


def test_redacts_nested_secret_keys() -> None:
    event = redact_secrets(
        None,
        "info",
        {"event": "call", "headers": {"authorization": "Bearer abc", "accept": "json"}},
    )
    assert event["headers"]["authorization"] == REDACTED_PLACEHOLDER
    assert event["headers"]["accept"] == "json"


def test_leaves_non_secret_values_untouched() -> None:
    event = redact_secrets(None, "info", {"event": "call", "model": "claude-sonnet-5"})
    assert event["model"] == "claude-sonnet-5"


def test_json_output_is_machine_readable(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level=LogLevel.INFO, log_format=LogFormat.JSON)
    get_logger("test").info("migration_started", repo="example", token="ghp_secret")

    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["event"] == "migration_started"
    assert payload["repo"] == "example"
    assert payload["token"] == REDACTED_PLACEHOLDER
    assert payload["level"] == "info"
    assert payload["logger_name"] == "test"
    assert "timestamp" in payload


def test_level_filtering_suppresses_debug(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level=LogLevel.WARNING, log_format=LogFormat.JSON)
    logger = get_logger("test")
    logger.debug("noisy")
    logger.warning("audible")

    captured = capsys.readouterr().err
    assert "noisy" not in captured
    assert "audible" in captured


def test_configure_from_settings_applies_settings(capsys: pytest.CaptureFixture[str]) -> None:
    configure_from_settings(
        Settings(log_level=LogLevel.DEBUG, log_format=LogFormat.JSON, _env_file=None)
    )
    get_logger("test").debug("detail_event")
    assert "detail_event" in capsys.readouterr().err


def test_configuration_is_idempotent() -> None:
    configure_logging(level=LogLevel.INFO, log_format=LogFormat.JSON)
    configure_logging(level=LogLevel.INFO, log_format=LogFormat.CONSOLE)
    assert structlog.is_configured()


def test_redaction_leaves_non_mapping_values_alone() -> None:
    event = redact_secrets(None, "info", {"event": "call", "attempts": [1, 2, 3]})
    assert event["attempts"] == [1, 2, 3]


def test_redacts_deeply_nested_secret_keys() -> None:
    event = redact_secrets(
        None,
        "info",
        {"event": "call", "request": {"auth": {"api_key": "sk-live-1"}, "retries": 2}},
    )
    assert event["request"]["auth"]["api_key"] == REDACTED_PLACEHOLDER
    assert event["request"]["retries"] == 2


def test_reconfiguration_affects_existing_loggers(capsys: pytest.CaptureFixture[str]) -> None:
    """Modules bind their logger at import, long before --verbose is parsed."""
    configure_logging(level=LogLevel.WARNING, log_format=LogFormat.JSON)
    logger = get_logger("test")
    logger.debug("before")
    assert "before" not in capsys.readouterr().err

    configure_logging(level=LogLevel.DEBUG, log_format=LogFormat.JSON)
    logger.debug("after")
    assert "after" in capsys.readouterr().err


def test_logging_follows_a_replaced_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """The logger must resolve sys.stderr per write, not snapshot it at setup.

    Binding the stream at configuration time left every later log call writing
    to whatever stream was installed when `configure_logging` ran — which, under
    a test harness that swaps streams between tests, is one that has since been
    closed.
    """
    import io
    import sys

    configure_logging(level=LogLevel.INFO, log_format=LogFormat.JSON)
    capsys.readouterr()

    replacement = io.StringIO()
    original, sys.stderr = sys.stderr, replacement
    try:
        get_logger("test").info("after_replacement")
    finally:
        sys.stderr = original

    assert "after_replacement" in replacement.getvalue()
