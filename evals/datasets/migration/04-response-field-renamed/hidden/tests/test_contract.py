from reader import build_payload, was_truncated


def test_the_reader_uses_the_new_response_field():
    assert was_truncated({"stop_reason": "length"})
    assert not was_truncated({"stop_reason": "stop"})


def test_the_request_payload_was_left_alone():
    assert set(build_payload("hi")) == {"model", "messages"}
