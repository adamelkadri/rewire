from partial import build_chat_payload
from partial.batch import build_batch_payload


def test_chat_payload_is_already_migrated():
    assert "max_completion_tokens" in build_chat_payload("hi")


def test_batch_payload_carries_a_limit():
    assert build_batch_payload(["hi"])["max_tokens"] == 64
