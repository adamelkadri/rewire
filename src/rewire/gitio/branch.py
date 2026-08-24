"""The write half of Git: branch, stage, commit, push.

Everything in :mod:`rewire.gitio.repository` reads. This writes, and the whole
module is shaped by one rule: **Rewire must never be able to destroy work it did
not create.**

That rule produces the restrictions here, and every one of them is a refusal
rather than a warning:

* **Only the patch's own files are staged.** ``git add -A`` would sweep the
  user's unrelated edits into Rewire's commit, and no amount of care elsewhere
  would get them back out. The file list comes from the patch.
* **A branch is never reused.** If the name exists, Rewire stops instead of
  adding a commit to something it did not create.
* **A push is never forced.** There is no flag for it. A rejected push means the
  remote has something Rewire has not seen, and overwriting that is exactly the
  irreversible act this module exists to prevent.
* **The original branch is restored** whatever happens, so a failure half way
  through leaves the user where they started rather than on a branch they did
  not ask for.

Nothing here merges, rebases, resets, cleans, stashes or checks out a path.
Those are the commands that lose work.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from rewire.core.errors import GitError
from rewire.core.logging import get_logger

logger = get_logger(__name__)

#: Seconds allowed for a local Git command.
GIT_TIMEOUT_SECONDS: Final[int] = 120

#: Seconds allowed for a push, which talks to a network.
PUSH_TIMEOUT_SECONDS: Final[int] = 300

#: Characters permitted in a generated branch name. Deliberately narrower than
#: Git allows: a name is built partly from specification text, and a leading
#: dash would be read as an option by every Git command that took it.
_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]+")

#: Longest generated branch name. Some hosts and filesystems dislike very long
#: refs, and a name nobody can read is no more useful than a short one.
MAX_BRANCH_LENGTH: Final[int] = 60


@dataclass(frozen=True, slots=True)
class Commit:
    """A commit Rewire created."""

    sha: str
    branch: str
    files: tuple[str, ...]
    message: str


def slugify(text: str, *, fallback: str = "migration") -> str:
    """Turn arbitrary text into something safe to use in a branch name.

    Leading dashes and dots are stripped rather than escaped: a ref beginning
    with a dash is read as a command-line option, and one beginning with a dot
    is rejected by Git outright.
    """
    cleaned = _UNSAFE.sub("-", text).strip("-.")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned[:MAX_BRANCH_LENGTH].strip("-.") or fallback


def branch_name(*, prefix: str = "rewire", subject: str = "", run_id: str = "") -> str:
    """Build a branch name that says what it is and cannot collide by accident.

    The run identifier is included so two migrations of the same specification
    do not produce the same name, which would make the second one refuse.
    """
    parts = [slugify(prefix), slugify(subject, fallback="migration")]
    if run_id:
        parts.append(slugify(run_id))
    return "/".join(part for part in parts if part)


def _git(
    root: Path, *args: str, timeout: int = GIT_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run one Git command inside ``root``.

    Raises:
        GitError: Git is missing or could not be executed. A non-zero exit is
            returned rather than raised, because several callers here treat it
            as an answer.
    """
    if shutil.which("git") is None:
        raise GitError("git was not found on PATH")
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), *args],  # noqa: S607 - resolved from PATH, as doctor checks
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"could not run git: {exc}", command=args[0] if args else "") from exc


def _require(result: subprocess.CompletedProcess[str], message: str) -> str:
    """Return a command's output, or raise with its error."""
    if result.returncode != 0:
        raise GitError(message, detail=(result.stderr or result.stdout).strip()[:300])
    return result.stdout.strip()


def current_branch(root: Path) -> str:
    """The checked-out branch, or an empty string on a detached HEAD."""
    result = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    name = result.stdout.strip() if result.returncode == 0 else ""
    return "" if name == "HEAD" else name


def branch_exists(root: Path, name: str) -> bool:
    """Whether a local branch of this name already exists."""
    return _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0


def default_remote(root: Path) -> str:
    """The name of the remote to push to, or an empty string if there is none."""
    remotes = _git(root, "remote").stdout.split()
    if not remotes:
        return ""
    return "origin" if "origin" in remotes else remotes[0]


def create_branch(root: Path, name: str) -> None:
    """Create ``name`` from the current HEAD and check it out.

    Raises:
        GitError: The name is already taken, or the branch could not be created.
            An existing branch is refused rather than reused: Rewire adding a
            commit to a branch it did not create is exactly the surprise this
            module exists to avoid.
    """
    if branch_exists(root, name):
        raise GitError(
            "branch already exists, and Rewire will not add commits to a branch it did not create",
            branch=name,
        )
    _require(_git(root, "checkout", "-b", name), f"could not create branch {name!r}")
    logger.info("git_branch_created", branch=name)


def checkout(root: Path, name: str) -> None:
    """Check out an existing branch.

    Raises:
        GitError: The branch could not be checked out.
    """
    _require(_git(root, "checkout", name), f"could not check out {name!r}")


def commit(root: Path, files: Sequence[str], message: str) -> Commit:
    """Stage exactly ``files`` and commit them.

    Only the named paths are staged. Anything else in the working tree — the
    user's own edits, build output, an unrelated fix in progress — is left
    untouched and uncommitted.

    Hooks are bypassed with ``--no-verify``, and the reason is correctness rather
    than convenience: a pre-commit hook may *rewrite files* — every formatter
    does — which would silently commit something other than the patch the sandbox
    verified. A repository's own hooks still run on the pull request through CI,
    where their failure is visible instead of silently absorbed.

    Raises:
        GitError: There is nothing to commit, or Git refused the operation.
    """
    if not files:
        raise GitError("refusing to commit: no files were given")

    # "--" separates paths from revisions, so a file named like a branch cannot
    # be reinterpreted as one.
    _require(_git(root, "add", "--", *files), "could not stage the patched files")

    staged = _git(root, "diff", "--cached", "--name-only").stdout.split()
    if not staged:
        raise GitError("refusing to commit: the patched files are identical to HEAD")

    _require(
        _git(root, "-c", "commit.gpgsign=false", "commit", "--no-verify", "--message", message),
        "could not create the commit",
    )
    sha = _require(_git(root, "rev-parse", "HEAD"), "could not read the new commit")
    logger.info("git_commit_created", sha=sha[:12], files=len(staged))
    return Commit(
        sha=sha, branch=current_branch(root), files=tuple(sorted(staged)), message=message
    )


def push(root: Path, remote: str, branch: str) -> None:
    """Push ``branch`` to ``remote``, never forcing.

    ``--force`` is not a parameter here and there is no flag that reaches it. A
    rejected push means the remote holds something Rewire has not seen, and
    overwriting it is the irreversible act this module exists to prevent.

    Raises:
        GitError: The push failed. The remote's message is included, because
            "the branch already exists on the remote" needs a different response
            from "you are not authenticated".
    """
    result = _git(root, "push", "--set-upstream", remote, branch, timeout=PUSH_TIMEOUT_SECONDS)
    _require(result, f"could not push {branch!r} to {remote!r}")
    logger.info("git_branch_pushed", remote=remote, branch=branch)


@contextmanager
def on_branch(root: Path, name: str) -> Iterator[None]:
    """Create and check out ``name``, restoring the original branch afterwards.

    The restore runs whatever happens. A failure half way through publishing
    leaves the user on the branch they started on rather than somewhere they did
    not ask to be — and the created branch survives, so nothing is lost.
    """
    original = current_branch(root)
    create_branch(root, name)
    try:
        yield
    finally:
        if original and current_branch(root) != original:
            restore = _git(root, "checkout", original)
            if restore.returncode != 0:  # pragma: no cover - needs a wedged checkout
                logger.warning(
                    "git_branch_not_restored", branch=original, detail=restore.stderr.strip()[:200]
                )


__all__ = [
    "GIT_TIMEOUT_SECONDS",
    "MAX_BRANCH_LENGTH",
    "PUSH_TIMEOUT_SECONDS",
    "Commit",
    "branch_exists",
    "branch_name",
    "checkout",
    "commit",
    "create_branch",
    "current_branch",
    "default_remote",
    "on_branch",
    "push",
    "slugify",
]
