"""Interpret a chat completion response."""

from typing import Any


def build_payload(prompt: str) -> dict[str, object]:
    """Build the JSON body. Unrelated to the response change."""
    return {"model": "gpt-4o", "messages": [{"role": "user", "content": prompt}]}


def was_truncated(response: dict[str, Any]) -> bool:
    """Whether the model stopped because it ran out of room."""
    return response["finish_reason"] == "length"
