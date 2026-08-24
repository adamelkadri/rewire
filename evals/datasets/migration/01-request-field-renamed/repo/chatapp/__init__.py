"""Build requests for the chat completions endpoint."""


def build_payload(prompt: str, limit: int = 256) -> dict[str, object]:
    """Build the JSON body sent to /v1/chat/completions."""
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": limit,
    }
