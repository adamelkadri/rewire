"""Tests for repository walking and its safety limits."""

from __future__ import annotations

from pathlib import Path

import pytest

from rewire.analyzers.discovery import (
    DiscoveryLimits,
    discover_python_files,
    is_test_path,
    module_path_for,
    resolve_repository_root,
)
from rewire.core.errors import RepositoryError


def build_tree(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def discovered(root: Path, limits: DiscoveryLimits | None = None) -> list[str]:
    return [file.relative_path for file in discover_python_files(root, limits).files]


# ------------------------------------------------------------------- roots ---


def test_resolve_root_canonicalises(tmp_path: Path) -> None:
    nested = tmp_path / "a" / ".." / "a"
    (tmp_path / "a").mkdir()
    assert resolve_repository_root(nested) == (tmp_path / "a").resolve()


def test_resolve_root_rejects_missing_paths(tmp_path: Path) -> None:
    with pytest.raises(RepositoryError, match="unusable"):
        resolve_repository_root(tmp_path / "nope")


def test_resolve_root_rejects_files(tmp_path: Path) -> None:
    target = tmp_path / "file.py"
    target.write_text("x = 1", encoding="utf-8")
    with pytest.raises(RepositoryError, match="not a directory"):
        resolve_repository_root(target)


# ----------------------------------------------------------------- walking ---


def test_finds_python_files_recursively(tmp_path: Path) -> None:
    build_tree(tmp_path, {"a.py": "", "pkg/b.py": "", "pkg/sub/c.py": "", "notes.md": ""})
    assert discovered(tmp_path) == ["a.py", "pkg/b.py", "pkg/sub/c.py"]


def test_results_are_sorted_deterministically(tmp_path: Path) -> None:
    build_tree(tmp_path, {f"m{i}.py": "" for i in range(10)})
    assert discovered(tmp_path) == sorted(discovered(tmp_path))


@pytest.mark.parametrize(
    "directory", [".git", ".venv", "node_modules", "__pycache__", "build", "dist", ".tox"]
)
def test_noise_directories_are_skipped(tmp_path: Path, directory: str) -> None:
    build_tree(tmp_path, {"keep.py": "", f"{directory}/skip.py": ""})
    assert discovered(tmp_path) == ["keep.py"]


def test_nested_site_packages_are_skipped(tmp_path: Path) -> None:
    build_tree(tmp_path, {"keep.py": "", ".venv/lib/python3.12/site-packages/dep/mod.py": ""})
    assert discovered(tmp_path) == ["keep.py"]


def test_symlinked_directories_are_not_followed(tmp_path: Path) -> None:
    """A link to '/' would walk the host filesystem; a link to '..' would loop."""
    build_tree(tmp_path, {"repo/a.py": "", "outside/secret.py": ""})
    (tmp_path / "repo" / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    assert discovered(tmp_path / "repo") == ["a.py"]


def test_symlinked_files_are_not_followed(tmp_path: Path) -> None:
    build_tree(tmp_path, {"repo/a.py": "", "outside/secret.py": ""})
    (tmp_path / "repo" / "linked.py").symlink_to(tmp_path / "outside" / "secret.py")
    assert discovered(tmp_path / "repo") == ["a.py"]


def test_unreadable_directories_do_not_abort_the_walk(tmp_path: Path) -> None:
    build_tree(tmp_path, {"a.py": "", "locked/b.py": ""})
    locked = tmp_path / "locked"
    locked.chmod(0o000)
    try:
        assert discovered(tmp_path) == ["a.py"]
    finally:
        locked.chmod(0o755)


# ------------------------------------------------------------------ limits ---


def test_oversized_files_are_skipped_with_a_reason(tmp_path: Path) -> None:
    build_tree(tmp_path, {"small.py": "x = 1", "big.py": "x = 1\n" * 1000})
    result = discover_python_files(tmp_path, DiscoveryLimits(max_file_bytes=100))
    assert [file.relative_path for file in result.files] == ["small.py"]
    assert "larger than" in result.skipped["big.py"]


def test_too_many_files_is_refused_not_truncated(tmp_path: Path) -> None:
    """Analysing part of a repository would report 'no usages' for unread code."""
    build_tree(tmp_path, {f"m{i}.py": "" for i in range(5)})
    with pytest.raises(RepositoryError, match="more Python files"):
        discover_python_files(tmp_path, DiscoveryLimits(max_files=3))


def test_total_size_limit_is_refused(tmp_path: Path) -> None:
    build_tree(tmp_path, {f"m{i}.py": "x" * 100 for i in range(5)})
    with pytest.raises(RepositoryError, match="total size limit"):
        discover_python_files(tmp_path, DiscoveryLimits(max_total_bytes=150))


def test_tests_can_be_excluded(tmp_path: Path) -> None:
    build_tree(tmp_path, {"app.py": "", "tests/test_app.py": ""})
    result = discover_python_files(tmp_path, DiscoveryLimits(include_tests=False))
    assert [file.relative_path for file in result.files] == ["app.py"]
    assert "tests/test_app.py" in result.skipped


def test_total_bytes_reported(tmp_path: Path) -> None:
    build_tree(tmp_path, {"a.py": "12345", "b.py": "123"})
    assert discover_python_files(tmp_path).total_bytes == 8


# ------------------------------------------------------------ classification -


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_a.py",
        "test/a.py",
        "src/tests/helpers.py",
        "test_module.py",
        "module_test.py",
        "conftest.py",
        "testing/fixtures.py",
    ],
)
def test_test_paths_are_recognised(path: str) -> None:
    assert is_test_path(path)


@pytest.mark.parametrize("path", ["src/app.py", "latest.py", "protest.py", "src/contest/rules.py"])
def test_source_paths_are_not_mistaken_for_tests(path: str) -> None:
    assert not is_test_path(path)


@pytest.mark.parametrize(
    ("path", "module"),
    [
        ("a.py", "a"),
        ("pkg/mod.py", "pkg.mod"),
        ("pkg/__init__.py", "pkg"),
        ("src/pkg/sub/mod.py", "src.pkg.sub.mod"),
        ("__init__.py", ""),
    ],
)
def test_module_paths(path: str, module: str) -> None:
    assert module_path_for(path) == module


def test_files_that_vanish_mid_walk_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file can be deleted between listing the directory and stat-ing it."""
    build_tree(tmp_path, {"a.py": "x = 1"})

    def _vanished(_path: Path) -> int:
        raise OSError("vanished")

    monkeypatch.setattr("rewire.analyzers.discovery._file_size", _vanished)
    result = discover_python_files(tmp_path)
    assert result.files == []
    assert "unreadable" in result.skipped["a.py"]
