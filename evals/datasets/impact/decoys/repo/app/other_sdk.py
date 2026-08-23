"""A different library that also takes a `max_tokens` argument.

Migrating the OpenAI spec must not rewrite calls into an unrelated package.
"""

import tiktoken


def truncate(text: str, max_tokens: int = 50) -> list[int]:
    encoding = tiktoken.get_encoding("cl100k_base")
    return encoding.encode(text)[:max_tokens]
