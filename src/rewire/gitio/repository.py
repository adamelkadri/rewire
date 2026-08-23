"""Read-only Git facts that decide whether Rewire may write.

Everything before Phase 7 was safe by construction: no command could modify the
user's repository, so nothing here was needed. ``rewire migrate --apply`` is the
first thing that writes, and writing needs one question answered honestly —
*can the user undo this?*

The answer is yes if, and only if, the change lands in a clean Git working tree.
Then ``git diff`` shows exactly what Rewire did and ``git checkout`` reverts it.
Into a tree that already has uncommitted edits, Rewire's changes and the user's
become one indistinguishable diff, and the undo is gone. That is why the check
exists, and why it refuses rather than warns.

Only ``git`` subcommands that read are used. Nothing here commits, stages,
checks out or stashes.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rewire.core.errors import GitError

#: Seconds allowed for a status query. Generous for a very large repository,
#: short enough that a hung Git does not hang the migration.
GIT_TIMEOUT_SECONDS = 60


@dataclass(frozen=True, slots=True)
class WorkingTree:
    """What Git says about a repository's current state."""

    root: Path
    #: ``False`` when the path is not inside a Git repository at all.
    is_repository: bool
    #: Paths with uncommitted modifications, staged or not. Empty when clean.
    dirty: tuple[str, ...] = ()
    branch: str = ""

    @property
    def is_clean(self) -> bool:
        """Whether a change written here would be visible in ``git diff`` alone."""
        return self.is_repository and not self.dirty

    def describe(self) -> str:
        """A one-line explanation suitable for a refusal message."""
        if not self.is_repository:
            return "not a Git repository, so a change written here could not be reviewed or undone"
        if self.dirty:
            shown = ", ".join(self.dirty[:5])
            more = f" and {len(self.dirty) - 5} more" if len(self.dirty) > 5 else ""
            return f"working tree has uncommitted changes: {shown}{more}"
        return f"clean working tree on {self.branch or 'a detached HEAD'}"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one read-only Git command inside ``root``.

    Raises:
        GitError: Git is missing, or could not be executed at all. A non-zero
            exit is returned to the caller rather than raised, because "this is
            not a repository" is an answer, not a failure.
    """
    if shutil.which("git") is None:
        raise GitError("git was not found on PATH")
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), *args],  # noqa: S607 - resolved from PATH, as doctor checks
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"could not run git: {exc}", command=" ".join(args)) from exc


def inspect_working_tree(root: Path | str) -> WorkingTree:
    """Report whether a repository is a clean Git checkout.

    Raises:
        GitError: Git is unavailable.
    """
    path = Path(root)
    inside = _git(path, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return WorkingTree(root=path, is_repository=False)

    status = _git(path, "status", "--porcelain")
    if status.returncode != 0:
        raise GitError("could not read the working tree state", detail=status.stderr.strip()[:200])

    # Porcelain v1: two status characters, a space, then the path. A rename
    # carries "old -> new"; the new name is the one that would be overwritten.
    dirty: list[str] = []
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        dirty.append(entry.split(" -> ")[-1].strip('"'))

    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    return WorkingTree(
        root=path,
        is_repository=True,
        dirty=tuple(sorted(dirty)),
        branch=branch.stdout.strip() if branch.returncode == 0 else "",
    )


__all__ = ["GIT_TIMEOUT_SECONDS", "WorkingTree", "inspect_working_tree"]
