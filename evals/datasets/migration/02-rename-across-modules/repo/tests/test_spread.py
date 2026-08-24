from spread import build_payload
from spread.budget import clamp
from spread.logging_helpers import describe


def test_clamp_lowers_an_oversized_limit():
    assert clamp(build_payload("hi", 99999))["max_tokens"] == 1024


def test_describe_mentions_the_limit():
    assert "limit=8" in describe(build_payload("hi", 8))
