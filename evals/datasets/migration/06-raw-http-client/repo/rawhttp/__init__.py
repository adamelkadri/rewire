"""A deliberately generic HTTP layer."""

from typing import Any

BASE_URL = "https://api.example.com"


def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Pretend to send a request. Returns the body it would have sent."""
    return {"url": BASE_URL + path, "json": body}


def complete(prompt: str, limit: int = 128) -> dict[str, Any]:
    """Ask the chat endpoint for a completion."""
    return post(
        "/v1/chat/completions",
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": limit,
        },
    )
