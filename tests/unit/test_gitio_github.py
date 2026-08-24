"""Tests for the GitHub layer, with ``gh`` replaced by a recording stub.

The subprocess is stubbed rather than run: these tests are about what Rewire
asks GitHub for, and the one thing that matters most is what it never asks for.
Nothing here touches the network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rewire.core.errors import GitError
from rewire.gitio import github

REPO_JSON = json.dumps(
    {"owner": {"login": "acme"}, "name": "widgets", "defaultBranchRef": {"name": "trunk"}}
)


class Recorder:
    """Stands in for ``gh``, remembering every argv it was handed."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(github.subprocess, "run", recorder)
    return recorder


# ---------------------------------------------------------------- absence ---


def test_the_module_exposes_no_way_to_merge() -> None:
    """Structural, not a policy: there is no function to call."""
    for forbidden in ("merge", "approve", "enable_auto_merge", "submit_review"):
        assert not hasattr(github, forbidden)


# ------------------------------------------------------------------- gh ---


def test_a_missing_gh_is_a_readable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github.shutil, "which", lambda _name: None)
    with pytest.raises(GitError, match="GitHub CLI"):
        github.describe_repository(Path())


def test_a_missing_gh_is_reported_as_unauthenticated_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The precheck asks a yes/no question and must answer it, not explode."""
    monkeypatch.setattr(github.shutil, "which", lambda _name: None)
    assert github.is_authenticated(Path()) is False


def test_a_gh_that_cannot_be_executed_is_a_domain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("no such file")

    monkeypatch.setattr(github.subprocess, "run", boom)
    with pytest.raises(GitError, match="could not run gh"):
        github.describe_repository(Path())


def test_authentication_is_reported_from_the_exit_code(gh: Recorder) -> None:
    assert github.is_authenticated(Path()) is True
    assert gh.calls[0][:3] == ["gh", "auth", "status"]

    gh.returncode = 1
    assert github.is_authenticated(Path()) is False


# ------------------------------------------------------------ repository ---


def test_the_repository_is_read_from_gh(gh: Recorder) -> None:
    gh.stdout = REPO_JSON
    repository = github.describe_repository(Path())
    assert repository.owner == "acme"
    assert repository.name == "widgets"
    assert repository.default_branch == "trunk"
    assert repository.slug == "acme/widgets"


def test_a_directory_with_no_github_remote_is_a_readable_error(gh: Recorder) -> None:
    gh.returncode = 1
    gh.stderr = "no git remotes found"
    with pytest.raises(GitError, match="could not identify the GitHub repository"):
        github.describe_repository(Path())


@pytest.mark.parametrize("payload", ["not json", "{}", '{"owner": {}}'])
def test_an_unreadable_description_is_a_readable_error(gh: Recorder, payload: str) -> None:
    gh.stdout = payload
    with pytest.raises(GitError, match="could not read the repository description"):
        github.describe_repository(Path())


# ---------------------------------------------------------- pull requests ---


def opened(gh: Recorder, **kwargs: object) -> github.PullRequest:
    gh.stdout = "https://github.com/acme/widgets/pull/42\n"
    defaults: dict[str, object] = {
        "title": "Migrate Example API to 2",
        "body": "why",
        "head": "rewire/x",
        "base": "main",
    }
    return github.open_pull_request(Path(), **{**defaults, **kwargs})  # type: ignore[arg-type]


def test_a_pull_request_is_created_with_head_and_base(gh: Recorder) -> None:
    result = opened(gh)
    argv = gh.calls[0]
    assert argv[:3] == ["gh", "pr", "create"]
    assert "--head" in argv and "rewire/x" in argv
    assert "--base" in argv and "main" in argv
    assert result.url.endswith("/pull/42")
    assert result.number == 42
    assert result.branch == "rewire/x"


def test_creating_a_pull_request_never_asks_to_merge_it(gh: Recorder) -> None:
    """The flags that would merge are absent from the argv, not merely unused."""
    opened(gh)
    argv = gh.calls[0]
    for forbidden in ("--merge", "--auto", "--squash", "--rebase", "--admin"):
        assert forbidden not in argv


def test_a_draft_is_requested_only_when_asked(gh: Recorder) -> None:
    opened(gh)
    assert "--draft" not in gh.calls[0]
    opened(gh, draft=True)
    assert "--draft" in gh.calls[1]


def test_a_very_long_body_is_truncated(gh: Recorder) -> None:
    """A description nobody will read is not worth failing the request over."""
    opened(gh, body="x" * (github.MAX_BODY_CHARS + 5_000))
    body = gh.calls[0][gh.calls[0].index("--body") + 1]
    assert len(body) == github.MAX_BODY_CHARS


def test_a_failed_creation_is_a_readable_error(gh: Recorder) -> None:
    gh.returncode = 1
    gh.stderr = "a pull request for branch rewire/x already exists"
    with pytest.raises(GitError, match="could not open the pull request"):
        opened(gh)


def test_output_without_a_number_still_yields_a_url(gh: Recorder) -> None:
    """`gh` sometimes prints warnings first, or a URL with no trailing number."""
    gh.stdout = "Warning: something\nhttps://github.com/acme/widgets/pull/new/rewire-x\n"
    result = github.open_pull_request(Path(), title="t", body="b", head="rewire/x", base="main")
    assert result.url.endswith("/rewire-x")
    assert result.number == 0
    assert result.describe() == result.url
