"""Opening a pull request, and nothing else.

**Rewire cannot merge.** There is no merge function in this module, no approve,
no auto-merge flag, and no review submission. That is a structural decision
rather than a policy one: a capability that does not exist cannot be reached by
a bug, a bad prompt, or a future flag added in a hurry.

The reasoning is the same one that shapes the sandbox. Rewire's evidence is that
a repository's own checks passed in a container. That is good evidence and it is
not the same as knowing the change is right — Phases 8 to 10 measured exactly how
often the two differ. A pull request puts a person between that evidence and the
default branch, and the person is the part of the system that can notice what the
tests do not cover.

The ``gh`` CLI is used rather than the REST API because it means **no new
credential**. The user has already authenticated it, its token never passes
through Rewire, and there is nothing here to leak into a log or a trace. The cost
is a dependency on a binary being installed, which ``rewire doctor`` reports.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from rewire.core.errors import GitError
from rewire.core.logging import get_logger

logger = get_logger(__name__)

#: Seconds allowed for a ``gh`` call, all of which talk to GitHub.
GH_TIMEOUT_SECONDS: Final[int] = 120

#: Longest pull request body. GitHub's own limit is larger; this keeps a
#: verbose diff from producing a description nobody will read.
MAX_BODY_CHARS: Final[int] = 60_000


@dataclass(frozen=True, slots=True)
class Repository:
    """The GitHub repository a working tree belongs to."""

    owner: str
    name: str
    default_branch: str

    @property
    def slug(self) -> str:
        """``owner/name``, as GitHub writes it."""
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class PullRequest:
    """A pull request Rewire opened."""

    url: str
    number: int = 0
    branch: str = ""

    def describe(self) -> str:
        """One line naming it, for a terminal and a log."""
        return f"#{self.number} {self.url}" if self.number else self.url


def _gh(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one ``gh`` command with the repository as its working directory.

    Raises:
        GitError: ``gh`` is not installed or could not be executed.
    """
    if shutil.which("gh") is None:
        raise GitError(
            "the GitHub CLI (gh) was not found on PATH",
            remedy="install it from https://cli.github.com and run 'gh auth login'",
        )
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["gh", *args],  # noqa: S607 - resolved from PATH, as doctor checks
            cwd=root,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"could not run gh: {exc}") from exc


def is_authenticated(root: Path) -> bool:
    """Whether ``gh`` has a usable credential.

    Checked before a migration runs rather than after it, so an unauthenticated
    user finds out in milliseconds instead of after paying for an agent run.
    """
    try:
        return _gh(root, "auth", "status").returncode == 0
    except GitError:
        return False


def describe_repository(root: Path) -> Repository:
    """Identify the GitHub repository this working tree belongs to.

    Raises:
        GitError: The directory has no GitHub remote, or ``gh`` could not
            describe it.
    """
    result = _gh(root, "repo", "view", "--json", "owner,name,defaultBranchRef")
    if result.returncode != 0:
        raise GitError(
            "could not identify the GitHub repository",
            detail=(result.stderr or result.stdout).strip()[:300],
        )
    try:
        payload: dict[str, Any] = json.loads(result.stdout)
        return Repository(
            owner=payload["owner"]["login"],
            name=payload["name"],
            default_branch=payload["defaultBranchRef"]["name"],
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GitError(f"could not read the repository description: {exc}") from exc


def open_pull_request(
    root: Path,
    *,
    title: str,
    body: str,
    head: str,
    base: str,
    draft: bool = False,
) -> PullRequest:
    """Open a pull request from ``head`` into ``base``.

    Nothing here merges it. ``gh pr create`` is the only GitHub write Rewire
    performs, and the flags that would merge, approve or enable auto-merge are
    not passed and have no parameter to arrive through.

    Args:
        root: The repository's working tree.
        title: One-line summary.
        body: The description, truncated to :data:`MAX_BODY_CHARS`.
        head: The branch holding the change.
        base: The branch it is proposed into.
        draft: Open as a draft, which cannot be merged until marked ready.

    Raises:
        GitError: The pull request could not be created.
    """
    argv = [
        "pr",
        "create",
        "--title",
        title,
        "--body",
        body[:MAX_BODY_CHARS],
        "--head",
        head,
        "--base",
        base,
    ]
    if draft:
        argv.append("--draft")

    result = _gh(root, *argv)
    if result.returncode != 0:
        raise GitError(
            "could not open the pull request",
            detail=(result.stderr or result.stdout).strip()[:300],
        )

    url = next(
        (line.strip() for line in reversed(result.stdout.splitlines()) if line.startswith("http")),
        result.stdout.strip(),
    )
    number = 0
    if url.rstrip("/").rsplit("/", 1)[-1].isdigit():
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
    logger.info("pull_request_opened", url=url, head=head, base=base, draft=draft)
    return PullRequest(url=url, number=number, branch=head)


__all__ = [
    "GH_TIMEOUT_SECONDS",
    "MAX_BODY_CHARS",
    "PullRequest",
    "Repository",
    "describe_repository",
    "is_authenticated",
    "open_pull_request",
]
