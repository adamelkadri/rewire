from removal import build_payload

ALLOWED = {"model", "messages"}


def test_the_removed_field_is_no_longer_sent():
    assert "best_of" not in build_payload("hi")


def test_nothing_else_was_dropped():
    assert set(build_payload("hi")) == ALLOWED
