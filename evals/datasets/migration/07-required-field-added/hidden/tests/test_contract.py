from required import build_payload


def test_the_now_required_field_is_sent():
    assert "stream" in build_payload("hi")
