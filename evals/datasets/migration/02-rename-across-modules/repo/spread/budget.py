"""Cost controls applied to an already-built payload."""

from typing import Any

HARD_CAP = 1024


def clamp(payload: dict[str, Any]) -> dict[str, Any]:
    """Lower the request's token limit to the hard cap."""
    capped = dict(payload)
    capped["max_tokens"] = min(int(capped.get("max_tokens", 0)), HARD_CAP)
    return capped
