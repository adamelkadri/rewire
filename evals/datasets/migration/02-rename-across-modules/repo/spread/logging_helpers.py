"""Structured logging for outgoing requests."""

from typing import Any


def describe(payload: dict[str, Any]) -> str:
    """One-line description of a request, for the log."""
    return f"model={payload['model']} limit={payload['max_tokens']}"
