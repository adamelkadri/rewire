"""A thin wrapper over the chat endpoint."""


def complete(prompt: str, max_tokens: int = 64) -> dict[str, object]:
    """Send a completion request.

    ``max_tokens`` is this function's own public parameter.
    """
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
