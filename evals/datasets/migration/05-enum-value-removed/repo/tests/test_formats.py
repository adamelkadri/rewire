from formats import build_payload


def test_plain_requests_ask_for_text():
    assert build_payload("hi")["response_format"] == "text"
