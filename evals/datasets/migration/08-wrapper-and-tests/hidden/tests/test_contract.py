import inspect

from wrapper import complete


def test_the_wire_field_was_renamed():
    assert complete("hi", 8)["max_completion_tokens"] == 8


def test_the_public_python_parameter_was_not_renamed():
    assert "max_tokens" in inspect.signature(complete).parameters
