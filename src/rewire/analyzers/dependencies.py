"""Reading declared dependencies from a repository's packaging metadata.

Rewire needs to know which third-party libraries a repository claims to use, so
that Phase 3 can weight a detected API change by whether the SDK it belongs to
is even a dependency.

``setup.py`` is deliberately not supported. Extracting its dependencies means
either executing it -- arbitrary code from an untrusted repository, which is the
thing the sandbox exists to prevent -- or pattern-matching it, which is wrong
often enough to be worse than an honest gap. Repositories using only ``setup.py``
report no dependencies, and the reason is visible here rather than silent.
"""

from __future__ import annotations

import re
import tomllib
from configparser import ConfigParser
from configparser import Error as ConfigParserError
from pathlib import Path
from typing import Any, Final

from rewire.analyzers.models import Dependency

#: Files consulted, in the order they are read.
DEPENDENCY_FILES: Final[tuple[str, ...]] = (
    "pyproject.toml",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements/base.txt",
    "requirements/dev.txt",
)

#: Largest metadata file that will be read.
MAX_METADATA_BYTES: Final[int] = 2 * 1024 * 1024

#: Splits a PEP 508 requirement into its name and everything that follows.
_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?P<extras>\[[^\]]*\])?\s*(?P<rest>.*)$")

#: requirements.txt lines that are options rather than requirements.
_OPTION_PREFIXES: Final[tuple[str, ...]] = ("-", "--")


def parse_requirement(line: str) -> tuple[str, str] | None:
    """Split a PEP 508 requirement string into ``(name, specifier)``.

    Returns ``None`` for anything that is not a requirement: comments, blank
    lines, pip options, and URL or path installs whose package name is not
    recoverable from the string alone.
    """
    text = line.split("#", maxsplit=1)[0].strip()
    if not text or text.startswith(_OPTION_PREFIXES):
        return None
    if "://" in text and "@" not in text:
        return None

    match = _REQUIREMENT.match(text)
    if match is None:
        return None
    rest = match.group("rest").strip()
    # A PEP 508 direct reference ('pkg @ https://...') keeps its name.
    specifier = "" if rest.startswith("@") else rest
    return match.group("name"), specifier


def _dependencies_from_list(
    entries: Any, *, source: str, is_optional: bool, extra: str | None = None
) -> list[Dependency]:
    if not isinstance(entries, list):
        return []
    found: list[Dependency] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        parsed = parse_requirement(entry)
        if parsed is None:
            continue
        name, specifier = parsed
        found.append(
            Dependency(
                name=name,
                specifier=specifier,
                source=source,
                is_optional=is_optional,
                extra=extra,
            )
        )
    return found


def parse_pyproject(content: bytes, *, source: str) -> list[Dependency]:
    """Extract dependencies from a ``pyproject.toml``.

    Supports both PEP 621 (``[project]``) and Poetry
    (``[tool.poetry.dependencies]``) layouts.
    """
    try:
        document = tomllib.loads(content.decode("utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, ValueError):
        return []

    found: list[Dependency] = []
    project = document.get("project")
    if isinstance(project, dict):
        found.extend(
            _dependencies_from_list(project.get("dependencies"), source=source, is_optional=False)
        )
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for extra, entries in sorted(optional.items()):
                found.extend(
                    _dependencies_from_list(
                        entries, source=source, is_optional=True, extra=str(extra)
                    )
                )

    poetry = (
        document.get("tool", {}).get("poetry") if isinstance(document.get("tool"), dict) else None
    )
    if isinstance(poetry, dict):
        found.extend(_poetry_dependencies(poetry, source=source))
    return found


def _poetry_dependencies(poetry: dict[str, Any], *, source: str) -> list[Dependency]:
    """Read Poetry's mapping-shaped dependency tables."""
    found: list[Dependency] = []
    groups: list[tuple[Any, bool, str | None]] = [
        (poetry.get("dependencies"), False, None),
        (poetry.get("dev-dependencies"), True, "dev"),
    ]
    group_table = poetry.get("group")
    if isinstance(group_table, dict):
        for group_name, group in sorted(group_table.items()):
            if isinstance(group, dict):
                groups.append((group.get("dependencies"), True, str(group_name)))

    for table, is_optional, extra in groups:
        if not isinstance(table, dict):
            continue
        for name, constraint in sorted(table.items()):
            if name == "python":
                continue  # The interpreter constraint is not a dependency.
            specifier = constraint if isinstance(constraint, str) else ""
            found.append(
                Dependency(
                    name=str(name),
                    specifier=specifier,
                    source=source,
                    is_optional=is_optional,
                    extra=extra,
                )
            )
    return found


def parse_requirements_txt(content: bytes, *, source: str) -> list[Dependency]:
    """Extract dependencies from a ``requirements``-style file."""
    is_dev = "dev" in Path(source).name.lower()
    found: list[Dependency] = []
    for line in content.decode("utf-8", errors="replace").splitlines():
        parsed = parse_requirement(line)
        if parsed is None:
            continue
        name, specifier = parsed
        found.append(Dependency(name=name, specifier=specifier, source=source, is_optional=is_dev))
    return found


def parse_setup_cfg(content: bytes, *, source: str) -> list[Dependency]:
    """Extract dependencies from a ``setup.cfg``."""
    parser = ConfigParser()
    try:
        parser.read_string(content.decode("utf-8", errors="replace"))
    except (ConfigParserError, ValueError):
        return []

    found: list[Dependency] = []
    if parser.has_option("options", "install_requires"):
        found.extend(
            _dependencies_from_list(
                parser.get("options", "install_requires").splitlines(),
                source=source,
                is_optional=False,
            )
        )
    if parser.has_section("options.extras_require"):
        for extra, entries in sorted(parser.items("options.extras_require")):
            found.extend(
                _dependencies_from_list(
                    entries.splitlines(), source=source, is_optional=True, extra=extra
                )
            )
    return found


def collect_dependencies(root: Path) -> list[Dependency]:
    """Read every recognised metadata file under ``root``.

    Results are deduplicated on ``(normalised name, extra)`` keeping the first
    declaration found, so a package listed in both ``pyproject.toml`` and a
    ``requirements.txt`` appears once.
    """
    found: list[Dependency] = []
    for relative in DEPENDENCY_FILES:
        path = root / relative
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_METADATA_BYTES:
                continue
            content = path.read_bytes()
        except OSError:
            continue

        if path.name == "pyproject.toml":
            found.extend(parse_pyproject(content, source=relative))
        elif path.name == "setup.cfg":
            found.extend(parse_setup_cfg(content, source=relative))
        else:
            found.extend(parse_requirements_txt(content, source=relative))

    seen: set[tuple[str, str | None]] = set()
    unique: list[Dependency] = []
    for dependency in found:
        key = (dependency.normalised_name, dependency.extra)
        if key not in seen:
            seen.add(key)
            unique.append(dependency)
    return sorted(unique, key=lambda item: (item.normalised_name, item.extra or ""))


__all__ = [
    "DEPENDENCY_FILES",
    "MAX_METADATA_BYTES",
    "collect_dependencies",
    "parse_pyproject",
    "parse_requirement",
    "parse_requirements_txt",
    "parse_setup_cfg",
]
