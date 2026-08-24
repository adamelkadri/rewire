from chatapp import build_payload


def test_payload_carries_the_model():
    assert build_payload("hi")["model"] == "gpt-4o"


def test_payload_carries_the_limit():
    assert build_payload("hi", 8)["max_tokens"] == 8
