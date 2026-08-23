"""Tests for the path-confined workspace.

Every path here arrives from a language model that has read untrusted files, so
these are security tests, not convenience tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rewire.agents.workspace import MAX_READ_BYTES, Workspace
from rewire.core.errors import RepositoryError


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("docs\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "dep.py").write_text("x = 1\n", encoding="utf-8")
    return Workspace.open(tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "../../etc/passwd",
        "/etc/passwd",
        "src/../../outside.py",
        "src/./../../outside.py",
    ],
)
def test_paths_outside_the_repository_are_refused(workspace: Workspace, path: str) -> None:
    with pytest.raises(RepositoryError):
        workspace.resolve(path)


def test_symlinks_pointing_outside_are_refused(tmp_path: Path) -> None:
    """`resolve()` follows the link, so one check covers traversal and symlinks."""
    repo, outside = tmp_path / "repo", tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (repo / "link.txt").symlink_to(outside / "secret.txt")

    with pytest.raises(RepositoryError, match="escapes the repository"):
        Workspace.open(repo).read("link.txt")


def test_the_repository_root_itself_resolves(workspace: Workspace) -> None:
    assert workspace.resolve(".") == workspace.root


def test_reading_returns_a_numbered_window(workspace: Workspace) -> None:
    result = workspace.read("src/app.py", start_line=2, limit=1)
    assert result.text == "two"
    assert (result.start_line, result.end_line, result.total_lines) == (2, 2, 3)
    assert result.truncated


def test_reading_past_the_end_is_not_truncated(workspace: Workspace) -> None:
    assert not workspace.read("src/app.py", start_line=1, limit=100).truncated


def test_read_full_is_lossless(tmp_path: Path) -> None:
    """A patch is built from this, so it must preserve the file byte for byte.

    An earlier version round-tripped through splitlines(), dropping the trailing
    newline, and every generated diff was then rejected by `git apply`.
    """
    for content in ("a = 1\n", "a = 1", "a = 1\n\n\n", "x\r\ny\r\n", "   spaced   \n"):
        target = tmp_path / "f.py"
        target.write_text(content, encoding="utf-8")
        assert Workspace.open(tmp_path).read_full("f.py") == content


def test_missing_files_are_reported(workspace: Workspace) -> None:
    with pytest.raises(RepositoryError, match="does not exist"):
        workspace.read("nope.py")


def test_binary_files_are_refused(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(RepositoryError, match="binary"):
        Workspace.open(tmp_path).read("blob.bin")


def test_oversized_files_are_refused(tmp_path: Path) -> None:
    (tmp_path / "big.py").write_text("x" * (MAX_READ_BYTES + 1), encoding="utf-8")
    with pytest.raises(RepositoryError, match="too large"):
        Workspace.open(tmp_path).read("big.py")


def test_listing_skips_dependency_trees(workspace: Workspace) -> None:
    files = workspace.list_files()
    assert "src/app.py" in files
    assert "README.md" in files
    assert not [path for path in files if path.startswith(".venv")]


def test_listing_a_subdirectory(workspace: Workspace) -> None:
    assert workspace.list_files("src") == ["src/app.py"]


def test_listing_a_missing_directory_is_reported(workspace: Workspace) -> None:
    with pytest.raises(RepositoryError, match="does not exist"):
        workspace.list_files("nope")


def test_listing_respects_a_limit(workspace: Workspace) -> None:
    assert len(workspace.list_files(limit=1)) == 1


def test_reads_are_tracked(workspace: Workspace) -> None:
    workspace.read("src/app.py")
    assert workspace.files_read == {"src/app.py"}
    assert workspace.bytes_read > 0


def test_exists_is_safe_on_escaping_paths(workspace: Workspace) -> None:
    assert workspace.exists("src/app.py")
    assert not workspace.exists("../outside.py")


def test_the_workspace_has_no_write_method() -> None:
    """A tool cannot modify a repository even if the model asks it to."""
    forbidden = {"write", "write_text", "write_file", "delete", "unlink", "apply"}
    assert forbidden.isdisjoint(dir(Workspace))
