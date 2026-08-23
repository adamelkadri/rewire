"""Tests for attributing an API specification to a Python package."""

from __future__ import annotations

from pathlib import Path

import pytest

from rewire.analyzers import RepositoryIndex, build_index
from rewire.impact.packages import infer_packages, resolve_packages, title_tokens


@pytest.fixture
def index(tmp_path: Path) -> RepositoryIndex:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "a"\nversion = "1"\ndependencies = ["openai", "httpx"]\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("import openai\nimport httpx\n", encoding="utf-8")
    return build_index(tmp_path)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("OpenAI API", ["openai"]),
        ("The Public REST API v1", []),
        ("Stripe Payments API", ["stripe", "payments"]),
        (None, []),
        ("", []),
        ("Go API", []),  # two-letter tokens match everything
    ],
)
def test_title_tokenisation(title: str | None, expected: list[str]) -> None:
    assert title_tokens(title) == expected


def test_infers_a_package_the_repository_uses(index: RepositoryIndex) -> None:
    assert infer_packages("OpenAI API", index) == ["openai"]


def test_infers_nothing_for_an_unrelated_api(index: RepositoryIndex) -> None:
    """A wrong guess is worse than no guess: it would misdirect every signal."""
    assert infer_packages("Stripe API", index) == []


def test_infers_nothing_without_a_title(index: RepositoryIndex) -> None:
    assert infer_packages(None, index) == []


def test_prefers_imported_names_over_merely_declared(tmp_path: Path) -> None:
    """A declared-but-unused dependency cannot be the source of a call site."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "a"\nversion = "1"\ndependencies = ["openai"]\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    # Declared but never imported: still returned, because it is the only match.
    assert infer_packages("OpenAI API", build_index(tmp_path)) == ["openai"]


def test_explicit_packages_win(index: RepositoryIndex) -> None:
    assert resolve_packages("OpenAI API", index, explicit=("custom",)) == ("custom",)


def test_explicit_packages_are_deduplicated(index: RepositoryIndex) -> None:
    assert resolve_packages(None, index, explicit=("a", "a", "b")) == ("a", "b")


def test_resolve_falls_back_to_inference(index: RepositoryIndex) -> None:
    assert resolve_packages("OpenAI API", index) == ("openai",)
