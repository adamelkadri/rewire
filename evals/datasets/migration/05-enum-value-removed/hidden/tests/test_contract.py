from formats import build_payload

ACCEPTED = {"text", "json_object", "srt"}


def test_structured_requests_use_an_accepted_value():
    assert build_payload("hi", structured=True)["response_format"] in ACCEPTED


def test_plain_requests_still_ask_for_text():
    assert build_payload("hi")["response_format"] == "text"
