"""Copying a repository somewhere it can safely be broken.

Verification writes: it applies a patch, creates a virtual environment and lets
a test suite scribble wherever it likes. None of that may happen in the user's
checkout, so every run works on a throwaway copy and the original is never
opened for writing at any point in the pipeline.

The copy is also where the size ceiling is enforced. A repository large enough
to fill the disk is refused before anything is executed, rather than discovered
half way through a container run.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from rewire.analyzers.discovery import DEFAULT_IGNORED_DIRECTORIES
from rewire.core.errors import SandboxError
from rewire.core.logging import get_logger

logger = get_logger(__name__)

#: Directories never copied. ``.git`` is excluded because verification does not
#: need history and it is routinely the largest thing in a repository.
STAGING_EXCLUDED: frozenset[str] = DEFAULT_IGNORED_DIRECTORIES | {
    ".git",
    ".rewire",
    ".rewire-venv",
}


@dataclass(frozen=True, slots=True)
class StagedRepository:
    """A disposable copy of a repository, and what was left out of it."""

    root: Path
    files: int
    bytes_copied: int
    #: Paths deliberately not copied, mapped to the reason.
    excluded: tuple[str, ...] = ()


def stage_repository(
    source: Path | str, destination: Path | str, *, max_bytes: int
) -> StagedRepository:
    """Copy a repository into ``destination``, skipping build and VCS trees.

    Symlinks are skipped entirely rather than copied or followed: a link into
    the host filesystem would give a sandboxed test suite a path out of the
    staged tree through the bind mount.

    Args:
        source: The repository to copy. Never modified.
        destination: An existing empty directory to copy into.
        max_bytes: Refuse repositories whose copied content exceeds this.

    Raises:
        SandboxError: The source is missing, unreadable, or too large.
    """
    source_root = Path(source).resolve()
    target_root = Path(destination)
    if not source_root.is_dir():
        raise SandboxError("repository does not exist", path=str(source_root))
    # Copying into the tree being copied walks its own output: the destination
    # reappears on the stack, is copied into itself, and the loop only stops
    # when the filesystem refuses the path length. It would also leave a second
    # copy of the repository where the checks would collect it.
    if source_root == target_root.resolve() or source_root in target_root.resolve().parents:
        raise SandboxError(
            "staging destination is inside the repository",
            path=str(source_root),
            destination=str(target_root),
        )

    files = 0
    copied = 0
    excluded: list[str] = []
    stack: list[Path] = [source_root]
    target_root.mkdir(parents=True, exist_ok=True)

    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError as exc:
            raise SandboxError(f"could not read directory: {exc}", path=str(current)) from exc

        for entry in entries:
            relative = entry.relative_to(source_root)
            if entry.is_symlink():
                excluded.append(f"{relative.as_posix()} (symlink)")
                continue
            if entry.is_dir():
                if entry.name in STAGING_EXCLUDED:
                    excluded.append(f"{relative.as_posix()}/ (ignored directory)")
                    continue
                (target_root / relative).mkdir(parents=True, exist_ok=True)
                stack.append(entry)
                continue
            if not entry.is_file():
                excluded.append(f"{relative.as_posix()} (not a regular file)")
                continue

            size = entry.stat().st_size
            if copied + size > max_bytes:
                raise SandboxError(
                    "repository is too large to verify",
                    path=str(source_root),
                    limit_bytes=max_bytes,
                )
            destination_path = target_root / relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(entry, destination_path)
            except OSError as exc:
                raise SandboxError(f"could not copy file: {exc}", path=relative.as_posix()) from exc
            files += 1
            copied += size

    logger.debug("sandbox.staged", files=files, bytes=copied, excluded=len(excluded))
    return StagedRepository(
        root=target_root, files=files, bytes_copied=copied, excluded=tuple(sorted(excluded))
    )


__all__ = ["STAGING_EXCLUDED", "StagedRepository", "stage_repository"]
