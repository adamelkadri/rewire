"""Structured logging configuration.

Rewire emits structured events rather than prose: every log line is a named
event plus typed key/value pairs, so agent runs stay queryable after the fact.
In ``console`` mode the output is human-readable; in ``json`` mode it is
line-delimited JSON suitable for shipping to a log store.

A redaction processor drops well-known secret-bearing keys before rendering, so
that an accidental ``log.info("call", api_key=...)`` cannot leak a credential.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from structlog.typing import EventDict, FilteringBoundLogger, WrappedLogger

from rewire.core.config import LogFormat, LogLevel, Settings

#: Keys whose values are never rendered, at any log level.
REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "anthropic_api_key",
        "openai_api_key",
        "openrouter_api_key",
        "authorization",
        "github_token",
        "token",
        "password",
        "secret",
        "access_token",
        "refresh_token",
        "client_secret",
        "private_key",
    }
)

REDACTED_PLACEHOLDER = "***redacted***"


def redact_secrets(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """Replace the value of any known secret-bearing key with a placeholder."""
    for key in list(event_dict):
        if key.lower() in REDACTED_KEYS:
            event_dict[key] = REDACTED_PLACEHOLDER
        elif isinstance(event_dict[key], MutableMapping):
            event_dict[key] = _redact_mapping(event_dict[key])
    return event_dict


def _redact_mapping(mapping: MutableMapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(key, str) and key.lower() in REDACTED_KEYS:
            result[key] = REDACTED_PLACEHOLDER
        elif isinstance(value, MutableMapping):
            result[key] = _redact_mapping(value)
        else:
            result[key] = value
    return result


class _CurrentStderr:
    """A file-like proxy that resolves ``sys.stderr`` on every write.

    ``WriteLoggerFactory`` captures the file object it is given, so passing
    ``sys.stderr`` directly binds whatever stream was installed at configuration
    time. Anything that later replaces it — a test harness capturing output, a
    caller redirecting streams — leaves the logger writing to a stream that may
    already be closed. Resolving it per write makes logging follow the current
    stderr, which is what a caller redirecting it expects.
    """

    def write(self, message: str) -> int:
        """Write to the current ``sys.stderr``."""
        return sys.stderr.write(message)

    def flush(self) -> None:
        """Flush the current ``sys.stderr``."""
        sys.stderr.flush()


def configure_logging(
    *,
    level: LogLevel = LogLevel.INFO,
    log_format: LogFormat = LogFormat.CONSOLE,
) -> None:
    """Configure ``structlog`` and the stdlib logging bridge.

    Idempotent: calling it again reconfigures cleanly, which matters for tests
    and for the CLI, where verbosity flags are resolved after settings load.

    Args:
        level: Minimum level to emit.
        log_format: ``console`` for human output, ``json`` for machine output.
    """
    numeric_level = logging.getLevelNamesMapping()[level.value]

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        redact_secrets,
    ]

    renderer: structlog.typing.Processor
    if log_format is LogFormat.JSON:
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.WriteLoggerFactory(file=_CurrentStderr()),  # type: ignore[arg-type]
        # Caching would freeze each logger against the configuration active the
        # first time it was used. Modules bind their logger at import, so a
        # later `configure_logging` -- which is exactly what `--verbose` does --
        # would silently have no effect on them.
        cache_logger_on_first_use=False,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=numeric_level,
        force=True,
    )


def configure_from_settings(settings: Settings) -> None:
    """Configure logging from an application :class:`Settings` instance."""
    configure_logging(level=settings.log_level, log_format=settings.log_format)


def get_logger(name: str) -> FilteringBoundLogger:
    """Return a structured logger bound to ``name``.

    The name is passed as an initial value rather than applied with ``.bind()``.
    ``bind()`` materialises structlog's lazy proxy immediately, freezing the
    logger against whatever configuration is active at that moment -- and
    modules call this at import, before :func:`configure_logging` has run. The
    result was a logger using structlog's defaults: console rendering onto
    *stdout*, ignoring ``REWIRE_LOG_LEVEL`` and ``REWIRE_LOG_FORMAT`` entirely,
    which corrupted every ``--json`` payload it interleaved with.

    Passing the name as an initial value keeps the proxy lazy, so configuration
    is resolved on the first call rather than at import. The key is
    ``logger_name`` rather than ``logger`` because structlog reserves the latter
    as a parameter of ``wrap_logger``.
    """
    logger: FilteringBoundLogger = structlog.get_logger(name, logger_name=name)
    return logger


__all__ = [
    "REDACTED_KEYS",
    "REDACTED_PLACEHOLDER",
    "configure_from_settings",
    "configure_logging",
    "get_logger",
    "redact_secrets",
]
