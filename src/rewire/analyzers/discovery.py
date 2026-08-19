"""Walking a repository to decide what to analyse.

Repositories are untrusted input. This module is where the limits live: what to
skip, how large a file may be, how many files to accept, and — importantly —
that symlinks are never followed. A repository containing ``link -> /`` would
otherwise walk the entire host filesystem, and one containing ``link -> ..``
would loop forever.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final

from rewire.core.errors import RepositoryError

#: Directories never descended into. Build output and dependency trees dwarf a
#: repository's own code and contain none of the call sites Rewire looks for.
DEFAULT_IGNORED_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {
        ".bzr",
        ".eggs",
        ".git",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "env",
        "htmlcov",
        "node_modules",
        "site-packages",
        "venv",
    }
)

#: Directory names that mark everything beneath them as test code.
TEST_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset({"test", "tests", "testing"})

#: Filenames that conventionally mark a module as a program entry point.
WELL_KNOWN_ENTRY_FILENAMES: Final[frozenset[str]] = frozenset(
    {"__main__.py", "main.py", "manage.py", "app.py", "wsgi.py", "asgi.py"}
)

DEFAULT_MAX_FILE_BYTES: Final[int] = 1024 * 1024
DEFAULT_MAX_FILES: Final[int] = 20_000
DEFAULT_MAX_TOTAL_BYTES: Final[int] = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DiscoveryLimits:
    """Bounds applied while walking a repository."""

    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_files: int = DEFAULT_MAX_FILES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    ignored_directories: frozenset[str] = DEFAULT_IGNORED_DIRECTORIES
    include_tests: bool = True


@dataclass(slots=True)
class DiscoveredFile:
    """One Python file selected for analysis."""

    path: Path
    relative_path: str
    size_bytes: int
    is_test: bool


@dataclass(slots=True)
class DiscoveryResult:
    """The outcome of a walk."""

    files: list[DiscoveredFile] = field(default_factory=list)
    #: Repository-relative paths that were found but deliberately not analysed,
    #: mapped to the reason. Reported rather than silently dropped, so that gaps
    #: in coverage are visible.
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        """Combined size of the selected files."""
        return sum(file.size_bytes for file in self.files)


def resolve_repository_root(path: Path | str) -> Path:
    """Validate and canonicalise a repository root.

    Raises:
        RepositoryError: The path does not exist or is not a directory.
    """
    root = Path(path).expanduser()
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RepositoryError(f"repository path is unusable: {exc}", path=str(root)) from exc
    if not resolved.is_dir():
        raise RepositoryError("repository path is not a directory", path=str(resolved))
    return resolved


def is_test_path(relative_path: str) -> bool:
    """Whether a repository-relative path looks like test code.

    Covers both conventions: a file named ``test_x.py``/``x_test.py``, and any
    file living under a ``tests/`` directory.
    """
    parts = PurePosixPath(relative_path).parts
    if any(part.lower() in TEST_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    stem = PurePosixPath(relative_path).stem
    return stem.startswith("test_") or stem.endswith("_test") or stem == "conftest"


def module_path_for(relative_path: str) -> str:
    """Derive a dotted module path from a repository-relative file path.

    ``src/pkg/mod.py`` becomes ``src.pkg.mod`` and ``pkg/__init__.py`` becomes
    ``pkg``. No attempt is made to strip source roots: the repository layout is
    not known, and a wrong guess would produce module paths that match nothing.
    """
    pure = PurePosixPath(relative_path)
    parts = list(pure.parts)
    parts[-1] = pure.stem
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _file_size(path: Path) -> int:
    """Return a file's size on disk.

    A separate function so that a walk racing against a deletion can be
    exercised in tests without stubbing every ``stat`` call the walker makes.
    """
    return path.stat().st_size


def _iter_python_files(root: Path, ignored: frozenset[str]) -> Iterator[Path]:
    """Yield Python files under ``root``, never following symlinks."""
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue  # Unreadable directory: skip rather than abort the walk.
        for entry in entries:
            if entry.is_symlink():
                # A symlink can point outside the repository or back into it.
                # Neither is worth the risk for the code it would add.
                continue
            if entry.is_dir():
                if entry.name not in ignored:
                    stack.append(entry)
            elif entry.suffix == ".py":
                yield entry


def discover_python_files(root: Path, limits: DiscoveryLimits | None = None) -> DiscoveryResult:
    """Walk ``root`` and select the Python files to analyse.

    Args:
        root: Canonical repository root, as returned by
            :func:`resolve_repository_root`.
        limits: Size and count bounds; defaults are used when omitted.

    Raises:
        RepositoryError: The repository exceeds ``max_files`` or
            ``max_total_bytes``. These are refusals, not truncations: silently
            analysing part of a repository would make Rewire report that it
            found no usages of an API that it never looked for.
    """
    limits = limits or DiscoveryLimits()
    result = DiscoveryResult()
    total_bytes = 0

    for path in sorted(_iter_python_files(root, limits.ignored_directories)):
        relative = path.relative_to(root).as_posix()
        try:
            size = _file_size(path)
        except OSError as exc:
            result.skipped[relative] = f"unreadable: {exc}"
            continue

        if size > limits.max_file_bytes:
            result.skipped[relative] = f"larger than {limits.max_file_bytes} bytes"
            continue

        is_test = is_test_path(relative)
        if is_test and not limits.include_tests:
            result.skipped[relative] = "test file excluded by configuration"
            continue

        total_bytes += size
        if len(result.files) >= limits.max_files:
            raise RepositoryError(
                "repository contains more Python files than the analyser accepts",
                path=str(root),
                limit=limits.max_files,
            )
        if total_bytes > limits.max_total_bytes:
            raise RepositoryError(
                "repository Python sources exceed the total size limit",
                path=str(root),
                limit_bytes=limits.max_total_bytes,
            )

        result.files.append(
            DiscoveredFile(path=path, relative_path=relative, size_bytes=size, is_test=is_test)
        )

    return result


__all__ = [
    "DEFAULT_IGNORED_DIRECTORIES",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "TEST_DIRECTORY_NAMES",
    "WELL_KNOWN_ENTRY_FILENAMES",
    "DiscoveredFile",
    "DiscoveryLimits",
    "DiscoveryResult",
    "discover_python_files",
    "is_test_path",
    "module_path_for",
    "resolve_repository_root",
]
