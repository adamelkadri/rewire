"""A path-confined, read-only view of a repository for agent tools.

Every path an agent tool receives comes from a language model, which in turn has
read repository content that Rewire does not control. Two distinct threats
follow, and both are handled here rather than in each tool:

* **Escape.** ``../../../../etc/passwd``, an absolute path, or a symlink
  pointing outside the tree. Every path is resolved and checked to be inside the
  root before it is opened.
* **Exhaustion.** A model asking for a 400 MB file, or reading the same file
  fifty times. Reads are capped and counted.

The workspace never writes. Edits are proposed as data (see
:mod:`rewire.agents.patch`) and applied elsewhere, so a tool call cannot modify
a repository even if the model asks it to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from rewire.analyzers.discovery import DEFAULT_IGNORED_DIRECTORIES, resolve_repository_root
from rewire.core.errors import RepositoryError

#: Largest file an agent may read in one call.
MAX_READ_BYTES: Final[int] = 256 * 1024

#: Most lines returned from a single read.
MAX_READ_LINES: Final[int] = 2_000

#: Most entries returned from a single listing.
MAX_LIST_ENTRIES: Final[int] = 500


@dataclass(slots=True)
class ReadResult:
    """The outcome of reading part of a file."""

    path: str
    text: str
    start_line: int
    end_line: int
    total_lines: int
    truncated: bool = False


@dataclass(slots=True)
class Workspace:
    """A repository the agent may inspect but not modify."""

    root: Path
    #: Files read so far, for reporting what the agent actually looked at.
    files_read: set[str] = field(default_factory=set)
    bytes_read: int = 0

    @classmethod
    def open(cls, path: Path | str) -> Workspace:
        """Open a repository, validating that it exists and is a directory."""
        return cls(root=resolve_repository_root(path))

    # ------------------------------------------------------------- paths ---

    def resolve(self, relative: str) -> Path:
        """Resolve a repository-relative path, refusing anything outside the root.

        Raises:
            RepositoryError: The path escapes the repository, is absolute, or
                traverses a symlink that leads outside it.
        """
        candidate = Path(relative)
        if candidate.is_absolute():
            raise RepositoryError("path must be repository-relative", path=relative)

        resolved = (self.root / candidate).resolve()
        # `resolve()` follows symlinks, so this single check covers traversal
        # via `..`, an absolute path, and a symlink pointing out of the tree.
        if resolved != self.root and self.root not in resolved.parents:
            raise RepositoryError("path escapes the repository", path=relative)
        return resolved

    def relative(self, path: Path) -> str:
        """Render an absolute path as a repository-relative POSIX path."""
        return path.relative_to(self.root).as_posix()

    # ------------------------------------------------------------ reading ---

    def _load(self, relative: str) -> str:
        """Read and validate a text file, returning its exact contents.

        Raises:
            RepositoryError: The file is missing, unreadable, too large, or
                binary.
        """
        path = self.resolve(relative)
        if not path.is_file():
            raise RepositoryError("file does not exist", path=relative)

        size = path.stat().st_size
        if size > MAX_READ_BYTES:
            raise RepositoryError(
                "file is too large to read", path=relative, size_bytes=size, limit=MAX_READ_BYTES
            )

        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise RepositoryError(f"could not read file: {exc}", path=relative) from exc
        if b"\x00" in raw[:8192]:
            raise RepositoryError("file appears to be binary", path=relative)

        self.files_read.add(relative)
        self.bytes_read += size
        return raw.decode("utf-8", errors="replace")

    def read(
        self, relative: str, *, start_line: int = 1, limit: int = MAX_READ_LINES
    ) -> ReadResult:
        """Read a slice of a text file, for display to the agent.

        Lossy by design: the returned text is a window of numbered lines, so
        trailing whitespace and the final newline do not survive. Never use it
        to build a patch — see :meth:`read_full`.

        Args:
            relative: Repository-relative path.
            start_line: First line to return, 1-indexed.
            limit: Most lines to return.

        Raises:
            RepositoryError: The file is missing, unreadable, too large, or
                binary.
        """
        lines = self._load(relative).splitlines()
        start = max(start_line, 1)
        window = lines[start - 1 : start - 1 + max(limit, 1)]

        return ReadResult(
            path=relative,
            text="\n".join(window),
            start_line=start,
            end_line=start + len(window) - 1,
            total_lines=len(lines),
            truncated=start - 1 + len(window) < len(lines),
        )

    def read_full(self, relative: str) -> str:
        r"""Read a file's exact contents, byte for byte after decoding.

        This is what a patch is built from, so it must be lossless. An earlier
        version round-tripped through ``splitlines()``, which silently dropped
        the trailing newline: every generated diff then claimed
        ``\\ No newline at end of file`` and was rejected by ``git apply``.
        """
        return self._load(relative)

    def exists(self, relative: str) -> bool:
        """Whether a repository-relative path points at an existing file."""
        try:
            return self.resolve(relative).is_file()
        except RepositoryError:
            return False

    # ------------------------------------------------------------ listing ---

    def list_files(self, directory: str = ".", *, limit: int = MAX_LIST_ENTRIES) -> list[str]:
        """List files under a directory, skipping build and dependency trees.

        Raises:
            RepositoryError: The directory escapes the repository or is missing.
        """
        target = self.resolve(directory)
        if not target.is_dir():
            raise RepositoryError("directory does not exist", path=directory)

        found: list[str] = []
        stack = [target]
        while stack and len(found) < limit:
            current = stack.pop()
            try:
                entries = sorted(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name not in DEFAULT_IGNORED_DIRECTORIES:
                        stack.append(entry)
                elif len(found) < limit:
                    found.append(self.relative(entry))
        return sorted(found)


__all__ = [
    "MAX_LIST_ENTRIES",
    "MAX_READ_BYTES",
    "MAX_READ_LINES",
    "ReadResult",
    "Workspace",
]
