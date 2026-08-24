from unrelated import build_embedding_request


def test_the_embedding_request_is_unchanged():
    assert build_embedding_request("hi") == {
        "model": "text-embedding-3-small",
        "input": "hi",
    }
