from spread import build_payload
from spread.budget import clamp
from spread.logging_helpers import describe

ALLOWED = {"model", "messages", "max_completion_tokens"}


def test_payload_uses_only_new_fields():
    assert set(build_payload("hi")) <= ALLOWED


def test_clamp_operates_on_the_new_field():
    assert clamp(build_payload("hi", 99999))["max_completion_tokens"] == 1024


def test_describe_still_reports_the_limit():
    assert "limit=8" in describe(build_payload("hi", 8))
