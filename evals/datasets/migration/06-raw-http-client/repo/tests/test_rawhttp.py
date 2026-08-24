from rawhttp import complete


def test_the_request_targets_the_chat_endpoint():
    assert complete("hi")["url"].endswith("/v1/chat/completions")
