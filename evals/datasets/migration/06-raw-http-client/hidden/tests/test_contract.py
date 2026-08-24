from rawhttp import complete

ALLOWED = {"model", "messages", "max_completion_tokens"}


def test_the_body_uses_only_accepted_fields():
    assert set(complete("hi")["json"]) <= ALLOWED


def test_the_limit_survives_the_rename():
    assert complete("hi", 8)["json"]["max_completion_tokens"] == 8
