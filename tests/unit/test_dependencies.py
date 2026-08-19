"""Tests for reading declared dependencies from packaging metadata."""

from __future__ import annotations

from pathlib import Path

import pytest

from rewire.analyzers.dependencies import (
    collect_dependencies,
    parse_pyproject,
    parse_requirement,
    parse_requirements_txt,
    parse_setup_cfg,
)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("openai", ("openai", "")),
        ("openai>=1.0", ("openai", ">=1.0")),
        ("openai >= 1.0, < 2", ("openai", ">= 1.0, < 2")),
        ("openai[datalib]>=1.0", ("openai", ">=1.0")),
        ("types-PyYAML", ("types-PyYAML", "")),
        ("pkg==1.0  # pinned", ("pkg", "==1.0")),
        ('pkg; python_version < "3.12"', ("pkg", '; python_version < "3.12"')),
        ("pkg @ https://example.test/pkg.whl", ("pkg", "")),
    ],
)
def test_requirement_parsing(line: str, expected: tuple[str, str]) -> None:
    assert parse_requirement(line) == expected


@pytest.mark.parametrize(
    "line",
    ["", "   ", "# a comment", "-e .", "--index-url https://example.test", "https://x.test/a.whl"],
)
def test_non_requirements_are_rejected(line: str) -> None:
    assert parse_requirement(line) is None


# --------------------------------------------------------------- pyproject ---


PEP621 = b"""
[project]
name = "app"
dependencies = ["openai>=1.40", "httpx"]

[project.optional-dependencies]
dev = ["pytest>=8"]
docs = ["sphinx"]
"""


def test_pep621_dependencies() -> None:
    found = {d.name: d for d in parse_pyproject(PEP621, source="pyproject.toml")}
    assert found["openai"].specifier == ">=1.40"
    assert not found["openai"].is_optional
    assert found["pytest"].is_optional
    assert found["pytest"].extra == "dev"
    assert found["sphinx"].extra == "docs"


POETRY = b"""
[tool.poetry]
name = "app"

[tool.poetry.dependencies]
python = "^3.12"
openai = "^1.40"
httpx = "*"

[tool.poetry.group.dev.dependencies]
pytest = "^8.0"
"""


def test_poetry_dependencies() -> None:
    found = {d.name: d for d in parse_pyproject(POETRY, source="pyproject.toml")}
    assert found["openai"].specifier == "^1.40"
    assert found["pytest"].is_optional
    assert found["pytest"].extra == "dev"


def test_poetry_python_constraint_is_not_a_dependency() -> None:
    """The interpreter version is a constraint on the environment, not a package."""
    assert "python" not in {d.name for d in parse_pyproject(POETRY, source="p.toml")}


def test_malformed_toml_yields_nothing() -> None:
    assert parse_pyproject(b"[project\nbroken", source="p.toml") == []


def test_pyproject_without_dependencies() -> None:
    assert parse_pyproject(b'[project]\nname = "app"\n', source="p.toml") == []


def test_non_string_dependency_entries_are_skipped() -> None:
    assert parse_pyproject(b"[project]\ndependencies = [1, 2]\n", source="p.toml") == []


# ------------------------------------------------------------ requirements ---


def test_requirements_txt() -> None:
    content = b"openai>=1.0\n# comment\n\nhttpx\n-e .\n"
    found = parse_requirements_txt(content, source="requirements.txt")
    assert [d.name for d in found] == ["openai", "httpx"]
    assert not found[0].is_optional


def test_dev_requirements_are_marked_optional() -> None:
    found = parse_requirements_txt(b"pytest\n", source="requirements-dev.txt")
    assert found[0].is_optional


# --------------------------------------------------------------- setup.cfg ---


SETUP_CFG = b"""
[options]
install_requires =
    openai>=1.0
    httpx

[options.extras_require]
dev =
    pytest
"""


def test_setup_cfg() -> None:
    found = {d.name: d for d in parse_setup_cfg(SETUP_CFG, source="setup.cfg")}
    assert found["openai"].specifier == ">=1.0"
    assert found["pytest"].is_optional
    assert found["pytest"].extra == "dev"


def test_malformed_setup_cfg_yields_nothing() -> None:
    assert parse_setup_cfg(b"[options\nbroken", source="setup.cfg") == []


# ---------------------------------------------------------------- collection -


def test_collect_reads_every_recognised_file(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_bytes(PEP621)
    (tmp_path / "requirements-dev.txt").write_text("mypy>=1.11\n", encoding="utf-8")
    names = {d.name for d in collect_dependencies(tmp_path)}
    assert {"openai", "httpx", "pytest", "sphinx", "mypy"} <= names


def test_collect_deduplicates_across_files(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_bytes(PEP621)
    (tmp_path / "requirements.txt").write_text("openai>=2.0\n", encoding="utf-8")
    openai = [d for d in collect_dependencies(tmp_path) if d.name == "openai"]
    assert len(openai) == 1
    assert openai[0].source == "pyproject.toml"  # first file wins


def test_collect_is_sorted(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("zzz\naaa\nmmm\n", encoding="utf-8")
    assert [d.name for d in collect_dependencies(tmp_path)] == ["aaa", "mmm", "zzz"]


def test_collect_on_an_empty_repository(tmp_path: Path) -> None:
    assert collect_dependencies(tmp_path) == []


def test_oversized_metadata_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rewire.analyzers.dependencies.MAX_METADATA_BYTES", 10)
    (tmp_path / "pyproject.toml").write_bytes(PEP621)
    assert collect_dependencies(tmp_path) == []


def test_setup_py_only_repositories_report_nothing(tmp_path: Path) -> None:
    """Executing setup.py is the exact risk the sandbox exists to avoid."""
    (tmp_path / "setup.py").write_text("setup(install_requires=['openai'])", encoding="utf-8")
    assert collect_dependencies(tmp_path) == []


def test_normalised_names_match_pep503() -> None:
    found = parse_requirements_txt(b"types_PyYAML\n", source="r.txt")
    assert found[0].normalised_name == "types-pyyaml"


def test_unparseable_requirement_lines_are_skipped() -> None:
    assert parse_requirement("!!!not-a-name!!!") is None


def test_collect_reads_setup_cfg(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_bytes(SETUP_CFG)
    assert {d.name for d in collect_dependencies(tmp_path)} == {"openai", "httpx", "pytest"}


def test_unreadable_metadata_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_bytes(PEP621)
    path.chmod(0o000)
    try:
        assert collect_dependencies(tmp_path) == []
    finally:
        path.chmod(0o644)


def test_non_mapping_poetry_group_is_skipped() -> None:
    content = b'[tool.poetry.dependencies]\nopenai = "^1"\n[tool.poetry]\ngroup = "not-a-table"\n'
    assert {d.name for d in parse_pyproject(content, source="p.toml")} == {"openai"}


def test_setup_cfg_without_extras() -> None:
    content = b"[options]\ninstall_requires =\n    openai\n"
    assert [d.name for d in parse_setup_cfg(content, source="setup.cfg")] == ["openai"]


def test_setup_cfg_without_install_requires() -> None:
    assert parse_setup_cfg(b"[metadata]\nname = app\n", source="setup.cfg") == []


def test_non_mapping_entry_inside_a_poetry_group_is_skipped() -> None:
    content = (
        b'[tool.poetry.dependencies]\nopenai = "^1"\n'
        b'[tool.poetry.group]\nbroken = "not-a-table"\n'
        b'[tool.poetry.group.dev.dependencies]\npytest = "^8"\n'
    )
    assert {d.name for d in parse_pyproject(content, source="p.toml")} == {"openai", "pytest"}
