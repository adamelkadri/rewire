from partial import build_chat_payload
from partial.batch import build_batch_payload

ALLOWED = {"model", "messages", "max_completion_tokens"}


def test_the_already_migrated_call_site_still_works():
    assert set(build_chat_payload("hi")) <= ALLOWED
    assert build_chat_payload("hi", 8)["max_completion_tokens"] == 8


def test_the_remaining_call_site_was_finished():
    assert set(build_batch_payload(["hi"])) <= ALLOWED
    assert build_batch_payload(["hi"], 8)["max_completion_tokens"] == 8
