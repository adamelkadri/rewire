"""A local budgeting helper. Its `max_tokens` has nothing to do with any API.

This module never imports the SDK. Every occurrence below is a decoy.
"""

DEFAULT_BUDGET = 4096


def clamp(requested: int, max_tokens: int = DEFAULT_BUDGET) -> int:
    """Local parameter that happens to share a name with the API field."""
    return min(requested, max_tokens)


def describe(limits: dict) -> str:
    # A dict key in an unrelated configuration structure.
    return f"budget={limits['max_tokens']}"


def log_line(count: int) -> str:
    # The field name inside a log message.
    return f"max_tokens exceeded: {count}"
