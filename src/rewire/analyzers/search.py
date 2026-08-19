"""Text search over a repository, as a fallback to AST analysis.

AST analysis is the primary way Rewire finds API usage, and it is strictly
better where it applies: it knows that ``max_tokens=`` is a keyword argument and
not a word in a comment. But it only applies to Python files that parse, and it
cannot see usages in templates, configuration, documentation or a language
Rewire does not yet understand.

Text search covers that remainder. Two interchangeable backends implement it:
ripgrep when it is installed, and a pure-Python scanner when it is not. The
Python backend is not a stub -- ripgrep is optional on purpose, and Rewire has
to work without it -- so both are held to the same result contract and tested
against each other.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from rewire.analyzers.discovery import DEFAULT_IGNORED_DIRECTORIES
from rewire.core.errors import RepositoryError

#: Wall-clock budget for one search. Bounded because ripgrep is an external
#: process operating on an untrusted directory tree.
SEARCH_TIMEOUT_SECONDS: Final[float] = 60.0

#: Default cap on returned matches, so a common word cannot flood the caller.
DEFAULT_MAX_MATCHES: Final[int] = 5_000

#: Longest line reported in full; longer ones are truncated in the preview only.
MAX_LINE_PREVIEW: Final[int] = 500


class TextMatch(BaseModel):
    """One matching line."""

    model_config = ConfigDict(frozen=True)

    file: str
    line: int
    column: int
    #: The matching line, stripped of trailing whitespace and truncated for
    #: display. Never used for further parsing.
    text: str

    @property
    def sort_key(self) -> tuple[str, int, int]:
        """Deterministic ordering across backends."""
        return (self.file, self.line, self.column)


class SearchBackend(ABC):
    """A way of finding a pattern in a directory tree."""

    name: str

    @abstractmethod
    def available(self) -> bool:
        """Whether this backend can run on this machine."""

    @abstractmethod
    def search(
        self,
        root: Path,
        pattern: str,
        *,
        regex: bool = False,
        globs: tuple[str, ...] = (),
        max_matches: int = DEFAULT_MAX_MATCHES,
    ) -> list[TextMatch]:
        """Find ``pattern`` under ``root``, returning matches in stable order."""


class RipgrepBackend(SearchBackend):
    """Search using the ``rg`` binary.

    ``--json`` is used rather than the human output format: paths containing
    colons would otherwise be indistinguishable from the field separator, and a
    repository is untrusted enough to contain one.
    """

    name = "ripgrep"

    def available(self) -> bool:
        """Whether ``rg`` is on PATH."""
        return shutil.which("rg") is not None

    def search(
        self,
        root: Path,
        pattern: str,
        *,
        regex: bool = False,
        globs: tuple[str, ...] = (),
        max_matches: int = DEFAULT_MAX_MATCHES,
    ) -> list[TextMatch]:
        """Find ``pattern`` under ``root`` by invoking ripgrep."""
        command = [
            "rg",
            "--json",
            "--no-follow",  # symlinks may escape the repository
            "--hidden",
            "--max-count",
            str(max_matches),
        ]
        if not regex:
            command.append("--fixed-strings")
        for directory in sorted(DEFAULT_IGNORED_DIRECTORIES):
            command.extend(["--glob", f"!{directory}/"])
        for glob in globs:
            command.extend(["--glob", glob])
        command.extend(["--", pattern, str(root)])

        try:
            completed = subprocess.run(  # noqa: S603 - argv list, no shell, pattern passed after --
                command,
                capture_output=True,
                text=True,
                timeout=SEARCH_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RepositoryError(
                "text search timed out", pattern=pattern, seconds=SEARCH_TIMEOUT_SECONDS
            ) from exc
        except OSError as exc:
            raise RepositoryError(f"could not run ripgrep: {exc}", pattern=pattern) from exc

        # ripgrep exits 1 to mean "no matches", which is not an error.
        if completed.returncode not in (0, 1):
            message = completed.stderr.strip().splitlines()
            raise RepositoryError(
                "ripgrep failed",
                pattern=pattern,
                reason=message[0] if message else f"exit code {completed.returncode}",
            )

        return sorted(
            self._parse(completed.stdout, root)[:max_matches],
            key=lambda match: match.sort_key,
        )

    @staticmethod
    def _parse(stdout: str, root: Path) -> list[TextMatch]:
        matches: list[TextMatch] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data", {})
            path_text = data.get("path", {}).get("text")
            submatches = data.get("submatches") or [{}]
            if not isinstance(path_text, str):
                continue  # A non-UTF-8 path; not something Rewire can migrate.
            try:
                relative = Path(path_text).relative_to(root).as_posix()
            except ValueError:
                relative = path_text
            matches.append(
                TextMatch(
                    file=relative,
                    line=int(data.get("line_number") or 0),
                    column=int(submatches[0].get("start", 0)) + 1,
                    text=(data.get("lines", {}).get("text") or "").rstrip()[:MAX_LINE_PREVIEW],
                )
            )
        return matches


class PythonBackend(SearchBackend):
    """Search implemented in pure Python, for machines without ripgrep.

    Slower than ripgrep on large trees, but always available and identical in
    what it reports. Binary files are detected by a NUL byte in the first block
    and skipped, matching ripgrep's own heuristic closely enough that the two
    backends agree on source trees.
    """

    name = "python"

    def available(self) -> bool:
        """Always true: this backend has no external requirements."""
        return True

    def search(
        self,
        root: Path,
        pattern: str,
        *,
        regex: bool = False,
        globs: tuple[str, ...] = (),
        max_matches: int = DEFAULT_MAX_MATCHES,
    ) -> list[TextMatch]:
        """Find ``pattern`` under ``root`` by scanning files directly."""
        try:
            compiled = re.compile(pattern if regex else re.escape(pattern))
        except re.error as exc:
            raise RepositoryError(f"invalid search pattern: {exc}", pattern=pattern) from exc

        matches: list[TextMatch] = []
        for path in self._walk(root):
            relative = path.relative_to(root).as_posix()
            if globs and not _matches_globs(relative, globs):
                continue
            matches.extend(self._search_file(path, relative, compiled))
            if len(matches) >= max_matches:
                break
        return sorted(matches[:max_matches], key=lambda match: match.sort_key)

    @staticmethod
    def _walk(root: Path) -> list[Path]:
        found: list[Path] = []
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name not in DEFAULT_IGNORED_DIRECTORIES:
                        stack.append(entry)
                else:
                    found.append(entry)
        return sorted(found)

    @staticmethod
    def _search_file(path: Path, relative: str, compiled: re.Pattern[str]) -> list[TextMatch]:
        try:
            raw = path.read_bytes()
        except OSError:
            return []
        if b"\x00" in raw[:8192]:
            return []  # Binary; ripgrep skips these too.

        found: list[TextMatch] = []
        for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
            match = compiled.search(line)
            if match is not None:
                found.append(
                    TextMatch(
                        file=relative,
                        line=number,
                        column=match.start() + 1,
                        text=line.rstrip()[:MAX_LINE_PREVIEW],
                    )
                )
        return found


def _matches_globs(relative: str, globs: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(relative, glob) or fnmatch(Path(relative).name, glob) for glob in globs)


def get_backend(preferred: str = "auto") -> SearchBackend:
    """Select a search backend.

    Args:
        preferred: ``auto`` picks ripgrep when installed and falls back to the
            Python scanner; ``ripgrep`` and ``python`` force a specific backend.

    Raises:
        RepositoryError: An unknown backend was named, or ripgrep was demanded
            and is not installed.
    """
    backends = {"ripgrep": RipgrepBackend(), "python": PythonBackend()}
    if preferred == "auto":
        ripgrep = backends["ripgrep"]
        return ripgrep if ripgrep.available() else backends["python"]

    backend = backends.get(preferred)
    if backend is None:
        raise RepositoryError(
            "unknown search backend", backend=preferred, available=sorted(backends)
        )
    if not backend.available():
        raise RepositoryError("requested search backend is not installed", backend=preferred)
    return backend


__all__ = [
    "DEFAULT_MAX_MATCHES",
    "MAX_LINE_PREVIEW",
    "SEARCH_TIMEOUT_SECONDS",
    "PythonBackend",
    "RipgrepBackend",
    "SearchBackend",
    "TextMatch",
    "get_backend",
]
