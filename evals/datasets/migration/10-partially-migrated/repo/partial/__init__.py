"""Already migrated by hand."""


def build_chat_payload(prompt: str, limit: int = 64) -> dict[str, object]:
    """Build the JSON body. This one is already correct."""
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": limit,
    }
