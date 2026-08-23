"""A tokenizer budget helper. Shares vocabulary with the API, uses none of it."""

MODEL_LIMITS = {"small": 1024, "large": 8192}


def budget(model: str, max_tokens: int | None = None) -> int:
    """`model` and `max_tokens` here are entirely local concepts."""
    limit = MODEL_LIMITS[model]
    return min(max_tokens or limit, limit)


def describe(usage: dict) -> str:
    return f"used {usage['completion_tokens']} of {usage['total_tokens']}"
