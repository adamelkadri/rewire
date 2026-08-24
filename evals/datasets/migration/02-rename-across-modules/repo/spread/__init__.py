"""Request construction."""

DEFAULT_LIMIT = 256


def build_payload(prompt: str, limit: int = DEFAULT_LIMIT) -> dict[str, object]:
    """Build the JSON body."""
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": limit,
    }
