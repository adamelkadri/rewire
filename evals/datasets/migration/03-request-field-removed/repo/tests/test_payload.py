from removal import build_payload


def test_payload_names_the_model():
    assert build_payload("hi")["model"] == "gpt-4o"
