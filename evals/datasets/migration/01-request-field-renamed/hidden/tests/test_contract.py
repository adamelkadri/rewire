"""Injected at grading time; the agent never sees this file."""

from chatapp import build_payload

ALLOWED = {"model", "messages", "max_completion_tokens"}


def test_payload_uses_only_fields_the_new_api_accepts():
    assert set(build_payload("hi")) <= ALLOWED


def test_the_limit_is_sent_under_the_new_name():
    assert build_payload("hi", 8)["max_completion_tokens"] == 8
