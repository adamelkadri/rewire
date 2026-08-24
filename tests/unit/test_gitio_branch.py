"""Tests for the write half of Git, against real repositories.

Real ``git`` rather than a mock, because what is being tested is precisely the
behaviour of the commands — that staging names files rather than everything,
that a branch is never reused, that a failure leaves the checkout where it
started. A mock would assert that Rewire *calls* the right commands, which is a
weaker claim than that the repository ends up in the right state.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from rewire.core.errors import GitError
from rewire.gitio.branch import (
    MAX_BRANCH_LENGTH,
    branch_exists,
    branch_name,
    checkout,
    commit,
    create_branch,
    current_branch,
    default_remote,
    on_branch,
    push,
    slugify,
)


def run(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args],  # noqa: S607 - git is on PATH in CI and locally
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real Git repository with one commit."""
    root = tmp_path / "repo"
    root.mkdir()
    run(root, "init", "--initial-branch=main")
    run(root, "config", "user.email", "test@example.com")
    run(root, "config", "user.name", "Test")
    # The developer running this may have a global commit-msg hook; the fixture
    # is not the place to satisfy it.
    run(root, "config", "core.hooksPath", "/dev/null")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "notes.txt").write_text("mine\n", encoding="utf-8")
    run(root, "add", "-A")
    run(root, "commit", "-m", "initial")
    return root


# ----------------------------------------------------------------- naming ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Example API", "Example-API"),
        ("v1 -> v2", "v1-v2"),
        ("--leading", "leading"),
        ("...dots...", "dots"),
        ("a/b\\c", "a-b-c"),
        ("", "migration"),
        ("///", "migration"),
    ],
)
def test_slugify_produces_a_usable_ref(text: str, expected: str) -> None:
    assert slugify(text) == expected


def test_a_slug_never_starts_with_a_dash() -> None:
    """A ref beginning with a dash is read as a command-line option."""
    assert not slugify("-rf /").startswith("-")


def test_a_slug_is_bounded() -> None:
    assert len(slugify("x" * 500)) <= MAX_BRANCH_LENGTH


def test_a_branch_name_carries_the_run_id() -> None:
    """Two migrations of one spec must not collide, or the second refuses."""
    first = branch_name(subject="Example API", run_id="abc123")
    second = branch_name(subject="Example API", run_id="def456")
    assert first != second
    assert first.startswith("rewire/")


# ------------------------------------------------------------- branching ---


def test_a_branch_is_created_and_checked_out(repo: Path) -> None:
    create_branch(repo, "rewire/test")
    assert current_branch(repo) == "rewire/test"
    assert branch_exists(repo, "rewire/test")


def test_an_existing_branch_is_refused_rather_than_reused(repo: Path) -> None:
    """Adding a commit to a branch Rewire did not create is the surprise to avoid."""
    create_branch(repo, "rewire/test")
    checkout(repo, "main")
    with pytest.raises(GitError, match="branch already exists"):
        create_branch(repo, "rewire/test")


def test_checking_out_a_missing_branch_fails_loudly(repo: Path) -> None:
    with pytest.raises(GitError, match="could not check out"):
        checkout(repo, "does-not-exist")


def test_the_original_branch_is_restored_after_success(repo: Path) -> None:
    with on_branch(repo, "rewire/test"):
        assert current_branch(repo) == "rewire/test"
    assert current_branch(repo) == "main"


def test_the_original_branch_is_restored_after_failure(repo: Path) -> None:
    """A half-finished publish must not leave the user somewhere they did not ask to be."""
    with pytest.raises(RuntimeError), on_branch(repo, "rewire/test"):
        raise RuntimeError("push failed")
    assert current_branch(repo) == "main"
    # The branch survives, so nothing that was done is lost.
    assert branch_exists(repo, "rewire/test")


def test_a_detached_head_reports_no_branch(repo: Path) -> None:
    run(repo, "checkout", "--detach")
    assert current_branch(repo) == ""


# ------------------------------------------------------------ committing ---


def test_only_the_named_files_are_committed(repo: Path) -> None:
    """The whole point. `git add -A` would sweep in the user's own work."""
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "notes.txt").write_text("my unrelated edit\n", encoding="utf-8")

    created = commit(repo, ["app.py"], "migrate app")

    assert created.files == ("app.py",)
    assert run(repo, "show", "--name-only", "--format=", "HEAD") == "app.py"
    # The user's edit is still there, still uncommitted.
    assert "notes.txt" in run(repo, "status", "--porcelain")
    assert (repo / "notes.txt").read_text(encoding="utf-8") == "my unrelated edit\n"


def test_committing_nothing_is_refused(repo: Path) -> None:
    with pytest.raises(GitError, match="no files were given"):
        commit(repo, [], "empty")


def test_committing_unchanged_files_is_refused(repo: Path) -> None:
    """An empty commit would claim work that did not happen."""
    with pytest.raises(GitError, match="identical to HEAD"):
        commit(repo, ["app.py"], "nothing changed")


def test_a_commit_records_its_branch_and_sha(repo: Path) -> None:
    (repo / "app.py").write_text("x = 3\n", encoding="utf-8")
    create_branch(repo, "rewire/test")
    created = commit(repo, ["app.py"], "migrate app")
    assert created.branch == "rewire/test"
    assert created.sha == run(repo, "rev-parse", "HEAD")
    assert created.message == "migrate app"


def test_a_hostile_filename_is_not_read_as_a_revision(repo: Path) -> None:
    """`--` separates paths from revisions, so a file named like a branch is safe."""
    weird = repo / "main"
    weird.write_text("not a branch\n", encoding="utf-8")
    created = commit(repo, ["main"], "add a file named main")
    assert created.files == ("main",)


# --------------------------------------------------------------- remotes ---


def test_a_repository_with_no_remote_reports_none(repo: Path) -> None:
    assert default_remote(repo) == ""


def test_origin_is_preferred(repo: Path) -> None:
    run(repo, "remote", "add", "upstream", "https://example.invalid/a.git")
    run(repo, "remote", "add", "origin", "https://example.invalid/b.git")
    assert default_remote(repo) == "origin"


def test_the_only_remote_is_used_when_it_is_not_origin(repo: Path) -> None:
    run(repo, "remote", "add", "upstream", "https://example.invalid/a.git")
    assert default_remote(repo) == "upstream"


# --------------------------------------------------------- what is absent ---


def test_the_module_offers_no_way_to_force_a_push() -> None:
    """The destructive Git subcommands are absent, not merely unused.

    Asserted over the module's string literals rather than its behaviour. A test
    that called one function would prove that one path does not force a push;
    this proves there is no path, because the argument does not exist anywhere in
    the module. Docstrings are excluded — the prose is allowed to name what the
    code must not do.
    """
    tree = ast.parse(Path("src/rewire/gitio/branch.py").read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } - docstrings

    forbidden = {"--force", "-f", "--hard", "reset", "clean", "stash", "rebase", "merge", "-A"}
    assert not (literals & forbidden), literals & forbidden


def test_a_missing_git_is_a_readable_error(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr("rewire.gitio.branch.shutil.which", lambda _name: None)
    with pytest.raises(GitError, match="git was not found"):
        current_branch(repo)


def test_a_git_that_cannot_be_executed_is_a_domain_error(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("exec format error")

    monkeypatch.setattr("rewire.gitio.branch.subprocess.run", boom)
    with pytest.raises(GitError, match="could not run git"):
        current_branch(repo)


def test_a_push_names_the_remote_and_branch_and_never_forces(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """The one command that reaches a network, checked argument by argument."""
    seen: list[list[str]] = []

    def record(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("rewire.gitio.branch.subprocess.run", record)
    push(repo, "origin", "rewire/test")

    argv = seen[0]
    assert argv[-4:] == ["push", "--set-upstream", "origin", "rewire/test"]
    assert "--force" not in argv
    assert "--force-with-lease" not in argv


def test_a_rejected_push_carries_the_remote_message(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """An already-existing branch needs a different response from a bad credential."""

    def reject(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "remote: Permission denied")

    monkeypatch.setattr("rewire.gitio.branch.subprocess.run", reject)
    with pytest.raises(GitError, match="Permission denied"):
        push(repo, "origin", "rewire/test")
