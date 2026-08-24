from wrapper import complete


def test_the_wrapper_accepts_a_limit():
    assert complete("hi", max_tokens=8)["max_tokens"] == 8
