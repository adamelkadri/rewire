"""Tests for the read-only Git facts that gate writing."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from rewire.core.errors import GitError
from rewire.gitio import inspect_working_tree
from rewire.gitio.repository import _git


def git(root: Path, *args: str) -> None:
    """Run git isolated from the developer's global hooks and config."""
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("value = 1\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "chore: initial")
    return root


def test_a_committed_tree_is_clean(repo: Path) -> None:
    tree = inspect_working_tree(repo)
    assert tree.is_repository
    assert tree.is_clean
    assert tree.dirty == ()
    assert "clean working tree" in tree.describe()


def test_a_modified_file_makes_the_tree_dirty(repo: Path) -> None:
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    tree = inspect_working_tree(repo)
    assert not tree.is_clean
    assert tree.dirty == ("a.py",)
    assert "a.py" in tree.describe()


def test_an_untracked_file_makes_the_tree_dirty(repo: Path) -> None:
    """An untracked file is still something `git checkout` will not undo."""
    (repo / "scratch.txt").write_text("notes\n", encoding="utf-8")
    assert inspect_working_tree(repo).dirty == ("scratch.txt",)


def test_a_staged_file_makes_the_tree_dirty(repo: Path) -> None:
    (repo / "b.py").write_text("value = 3\n", encoding="utf-8")
    git(repo, "add", "b.py")
    assert inspect_working_tree(repo).dirty == ("b.py",)


def test_a_rename_reports_the_new_name(repo: Path) -> None:
    """The new name is the path a patch would overwrite."""
    git(repo, "mv", "a.py", "c.py")
    assert inspect_working_tree(repo).dirty == ("c.py",)


def test_a_quoted_path_is_unquoted(repo: Path) -> None:
    """Git quotes unusual filenames; the raw quotes would not match a patch path."""
    (repo / "sp ace.py").write_text("value = 1\n", encoding="utf-8")
    assert "sp ace.py" in inspect_working_tree(repo).dirty


def test_the_branch_is_reported(repo: Path) -> None:
    assert inspect_working_tree(repo).branch in {"main", "master"}


def test_a_plain_directory_is_not_a_repository(tmp_path: Path) -> None:
    tree = inspect_working_tree(tmp_path)
    assert not tree.is_repository
    assert not tree.is_clean
    assert "not a Git repository" in tree.describe()


def test_many_dirty_files_are_summarised(repo: Path) -> None:
    for index in range(9):
        (repo / f"f{index}.py").write_text("x\n", encoding="utf-8")
    described = inspect_working_tree(repo).describe()
    assert "and 4 more" in described


def test_a_missing_git_binary_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(GitError, match="not found on PATH"):
        inspect_working_tree(tmp_path)


def test_git_failing_to_start_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", explode)
    with pytest.raises(GitError, match="could not run git"):
        _git(tmp_path, "status")


def test_an_unreadable_status_is_a_clear_error(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = subprocess.run

    def fail_status(argv: list[str], **kwargs: object):
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: bad object")
        return real(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", fail_status)
    with pytest.raises(GitError, match="could not read the working tree state"):
        inspect_working_tree(repo)


def test_a_detached_head_is_still_usable(repo: Path) -> None:
    """Detached is unusual but not unsafe; only dirtiness blocks writing."""
    git(repo, "checkout", "-q", "--detach")
    tree = inspect_working_tree(repo)
    assert tree.is_clean
    assert "detached HEAD" in tree.describe() or tree.branch == "HEAD"


def test_a_truncated_status_line_is_ignored(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Porcelain output is stable, but a short line must not crash the gate."""
    real = subprocess.run

    def short_status(argv: list[str], **kwargs: object):
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, "??\n M a.py\n", "")
        return real(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", short_status)
    assert inspect_working_tree(repo).dirty == ("a.py",)
