"""Test doubles that *construct* a usage block.

Constructing a response is not the same as depending on the server to send one.
A field becoming optional does not break code that writes it, so these are
decoys for a response-side change.
"""


def fake_usage() -> dict:
    return {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


def make_response(completion_tokens: int = 0) -> dict:
    return {"usage": {"completion_tokens": completion_tokens}}
