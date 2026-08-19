"""Tests for the domain exception hierarchy."""

from __future__ import annotations

import pytest

from rewire.core import errors


def test_all_domain_errors_derive_from_base() -> None:
    exported = [getattr(errors, name) for name in errors.__all__]
    subclasses = [obj for obj in exported if isinstance(obj, type)]
    assert subclasses, "expected exception classes in __all__"
    for cls in subclasses:
        assert issubclass(cls, errors.RewireError)


def test_error_codes_are_unique_and_stable() -> None:
    codes = [getattr(errors, name).code for name in errors.__all__]
    assert len(codes) == len(set(codes)), "duplicate error codes would break API clients"


def test_details_are_rendered_in_str() -> None:
    exc = errors.SpecParseError("could not parse spec", path="old.yaml", line=12)
    rendered = str(exc)
    assert "could not parse spec" in rendered
    assert "line=12" in rendered
    assert "path='old.yaml'" in rendered


def test_message_only_error_renders_plainly() -> None:
    assert str(errors.RewireError("boom")) == "boom"


def test_to_dict_is_json_serialisable() -> None:
    exc = errors.SandboxTimeoutError("timed out", seconds=600)
    assert exc.to_dict() == {
        "code": "sandbox_timeout_error",
        "message": "timed out",
        "details": {"seconds": 600},
    }


def test_specific_errors_are_catchable_as_base() -> None:
    with pytest.raises(errors.RewireError):
        raise errors.GitError("detached head")


def test_subclass_specialisation_is_preserved() -> None:
    assert issubclass(errors.SandboxTimeoutError, errors.SandboxError)
    assert issubclass(errors.LLMRateLimitError, errors.LLMError)
