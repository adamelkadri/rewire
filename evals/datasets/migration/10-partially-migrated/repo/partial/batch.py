"""Not yet migrated."""


def build_batch_payload(prompts: list[str], limit: int = 64) -> dict[str, object]:
    """Build the JSON body for a batch of prompts."""
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": p} for p in prompts],
        "max_tokens": limit,
    }
