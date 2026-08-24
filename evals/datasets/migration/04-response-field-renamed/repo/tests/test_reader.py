from reader import build_payload, was_truncated


def test_truncation_is_detected():
    assert was_truncated({"finish_reason": "length"})


def test_payload_names_the_model():
    assert build_payload("hi")["model"] == "gpt-4o"
