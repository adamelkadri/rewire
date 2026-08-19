"""Building a queryable index of a repository.

Ties discovery, AST analysis, dependency parsing and entry-point detection into
one deterministic pass. Determinism matters more than it might seem: the index
feeds impact analysis, which feeds the agent, and a nondeterministic index would
make every downstream evaluation number unreproducible. Files are therefore
walked in sorted order and every collection is sorted before it is stored.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

from rewire.analyzers.dependencies import collect_dependencies
from rewire.analyzers.discovery import (
    WELL_KNOWN_ENTRY_FILENAMES,
    DiscoveryLimits,
    discover_python_files,
    module_path_for,
    resolve_repository_root,
)
from rewire.analyzers.models import (
    EntryPoint,
    EntryPointKind,
    FileInfo,
    IndexStats,
    RepositoryIndex,
)
from rewire.analyzers.python_ast import analyse_source
from rewire.core.logging import get_logger

logger = get_logger(__name__)

#: Resolved constructors whose result is a web application object. Assigning one
#: at module level is the conventional shape of an ASGI/WSGI entry point.
WEB_APPLICATION_FACTORIES: Final[frozenset[str]] = frozenset(
    {
        "fastapi.FastAPI",
        "flask.Flask",
        "starlette.applications.Starlette",
        "django.core.wsgi.get_wsgi_application",
        "django.core.asgi.get_asgi_application",
    }
)


def build_index(path: Path | str, *, limits: DiscoveryLimits | None = None) -> RepositoryIndex:
    """Analyse a repository and return a queryable index.

    Args:
        path: Repository root. Resolved and validated before anything is read.
        limits: Discovery bounds; defaults are used when omitted.

    Raises:
        RepositoryError: The path is unusable, or the repository exceeds the
            configured size limits.
    """
    started = time.perf_counter()
    root = resolve_repository_root(path)
    discovered = discover_python_files(root, limits)

    files: list[FileInfo] = []
    for candidate in discovered.files:
        try:
            source = candidate.path.read_bytes()
        except OSError as exc:
            discovered.skipped[candidate.relative_path] = f"unreadable: {exc}"
            continue
        files.append(
            analyse_source(
                source,
                path=candidate.relative_path,
                module=module_path_for(candidate.relative_path),
                is_test=candidate.is_test,
                size_bytes=candidate.size_bytes,
            )
        )

    duration = time.perf_counter() - started
    index = RepositoryIndex(
        root=str(root),
        files=tuple(files),
        dependencies=tuple(collect_dependencies(root)),
        entry_points=tuple(detect_entry_points(root, files)),
        stats=IndexStats.from_files(files, skipped=len(discovered.skipped), duration=duration),
    )
    logger.info(
        "repository_indexed",
        root=str(root),
        files=index.stats.files_indexed,
        failed=index.stats.files_failed,
        symbols=index.stats.symbols,
        duration_seconds=round(duration, 4),
    )
    return index


# -------------------------------------------------------------- entry points ---


def detect_entry_points(root: Path, files: list[FileInfo]) -> list[EntryPoint]:
    """Identify plausible ways the repository's code is executed.

    Four independent signals are used. Each is reported separately rather than
    merged, because they carry different confidence: a declared console script
    is a fact, while a file named ``app.py`` is a guess.
    """
    found: list[EntryPoint] = []
    found.extend(_console_scripts(root))
    found.extend(_main_guards(files))
    found.extend(_well_known_filenames(files))
    found.extend(_web_applications(files))
    return sorted(found, key=lambda item: (item.file, item.kind.value, item.name or ""))


def _console_scripts(root: Path) -> list[EntryPoint]:
    import tomllib

    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return []

    project = document.get("project")
    scripts = project.get("scripts") if isinstance(project, dict) else None
    if not isinstance(scripts, dict):
        return []
    return [
        EntryPoint(
            kind=EntryPointKind.CONSOLE_SCRIPT,
            file="pyproject.toml",
            name=str(name),
            detail=str(target),
        )
        for name, target in sorted(scripts.items())
    ]


def _main_guards(files: list[FileInfo]) -> list[EntryPoint]:
    """Find ``if __name__ == "__main__":`` blocks.

    Detected from the recorded references rather than by reparsing: the analyser
    already saw the ``__name__`` name and the ``"__main__"`` string constant on
    the same line, and requiring both on one line is a precise enough test.
    """
    found: list[EntryPoint] = []
    for file in files:
        name_lines = {
            reference.line for reference in file.references if reference.name == "__name__"
        }
        guard_lines = sorted(
            reference.line
            for reference in file.references
            if reference.name == "__main__" and reference.line in name_lines
        )
        found.extend(
            EntryPoint(
                kind=EntryPointKind.MAIN_GUARD,
                file=file.path,
                line=line,
                detail="if __name__ == '__main__'",
            )
            for line in guard_lines
        )
    return found


def _well_known_filenames(files: list[FileInfo]) -> list[EntryPoint]:
    return [
        EntryPoint(
            kind=EntryPointKind.WELL_KNOWN_FILENAME,
            file=file.path,
            name=Path(file.path).name,
            detail="conventional entry-point filename",
        )
        for file in files
        if Path(file.path).name in WELL_KNOWN_ENTRY_FILENAMES and not file.is_test
    ]


def _web_applications(files: list[FileInfo]) -> list[EntryPoint]:
    """Find module-level assignments of a web application object."""
    found: list[EntryPoint] = []
    for file in files:
        module_level = {
            symbol.line: symbol.name for symbol in file.symbols if symbol.enclosing_symbol is None
        }
        for call in file.calls:
            if call.resolved_callee in WEB_APPLICATION_FACTORIES and call.line in module_level:
                found.append(
                    EntryPoint(
                        kind=EntryPointKind.WEB_APPLICATION,
                        file=file.path,
                        line=call.line,
                        name=module_level[call.line],
                        detail=call.resolved_callee or call.callee,
                    )
                )
    return found


def source_line(root: Path, file: str, line: int) -> str | None:
    """Return one line of source, for rendering a match in context.

    Returns ``None`` rather than raising when the file has changed or gone away
    since indexing; a missing context line is a display problem, not a failure.
    """
    path = Path(root) / file
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for number, text in enumerate(handle, start=1):
                if number == line:
                    return text.rstrip("\n")
    except OSError:
        return None
    return None


__all__ = [
    "WEB_APPLICATION_FACTORIES",
    "build_index",
    "detect_entry_points",
    "source_line",
]
