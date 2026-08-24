from unrelated import build_embedding_request


def test_the_request_names_the_embedding_model():
    assert build_embedding_request("hi")["model"] == "text-embedding-3-small"
