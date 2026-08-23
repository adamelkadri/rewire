"""Deciding which checks a repository actually supports.

Rewire runs the checks the repository configures, not a fixed list it wishes
were there. A project without mypy configured is not failing typecheck — it has
no typecheck, and a report that says otherwise is noise.

Detection is deliberately deterministic and file-based: it reads packaging
metadata and looks for directories. It never executes anything, so it is safe
to run on an untrusted repository before the sandbox exists.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from rewire.sandbox.models import CheckKind, CheckResult

#: Where the staged repository is mounted inside the container.
WORKSPACE_DIR: Final[str] = "/workspace"

#: Virtual environment created by the install step, inside the staged copy so
#: that it is thrown away with it and never touches the user's checkout.
VENV_DIR: Final[str] = f"{WORKSPACE_DIR}/.rewire-venv"

#: ``PATH`` inside the container. The virtual environment comes first, so a
#: bare ``python`` resolves to it once the install step has run and to the
#: image's interpreter before that. Nonexistent entries are simply ignored.
CONTAINER_PATH: Final[str] = (
    f"{VENV_DIR}/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
)

#: Directory names that mean the repository has a test suite.
TEST_DIRECTORIES: Final[frozenset[str]] = frozenset({"test", "tests"})

#: Tools installed alongside the project so the detected checks can run.
CHECK_PACKAGES: Final[dict[CheckKind, str]] = {
    CheckKind.TESTS: "pytest",
    CheckKind.LINT: "ruff",
    CheckKind.TYPECHECK: "mypy",
}


@dataclass(frozen=True, slots=True)
class Check:
    """One command to run, and why it was chosen."""

    kind: CheckKind
    name: str
    command: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ToolchainPlan:
    """The full set of commands a verification run will execute."""

    checks: tuple[Check, ...] = ()
    install: tuple[Check, ...] = ()
    #: Checks that were considered and rejected, with the reason. Reported
    #: rather than dropped, so that a thin report is visibly thin.
    skipped: tuple[CheckResult, ...] = ()

    @property
    def kinds(self) -> frozenset[CheckKind]:
        """The kinds of check this plan will actually run."""
        return frozenset(check.kind for check in self.checks)


def _read_pyproject(root: Path) -> dict[str, object]:
    """Parse ``pyproject.toml`` if it exists and is valid, else return empty."""
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        # A malformed pyproject is the repository's problem, not a reason to
        # abandon verification: the remaining checks are still worth running.
        return {}


def _tool_table(pyproject: dict[str, object], tool: str) -> bool:
    """Whether ``[tool.<tool>]`` is present in the parsed pyproject."""
    tools = pyproject.get("tool")
    return isinstance(tools, dict) and tool in tools


def has_tests(root: Path) -> bool:
    """Whether the repository contains anything pytest would collect."""
    for name in sorted(TEST_DIRECTORIES):
        candidate = root / name
        if candidate.is_dir() and any(candidate.rglob("test_*.py")):
            return True
    return any(root.glob("test_*.py")) or any(root.glob("*_test.py"))


def detect_install(root: Path, kinds: frozenset[CheckKind]) -> tuple[Check, ...]:
    """Build the commands that make the repository importable and checkable.

    Returns an empty tuple when there is nothing to install, which keeps the
    sandbox entirely offline for repositories that declare no dependencies.
    """
    targets: list[str] = []
    if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
        targets.append("--editable")
        targets.append(".")
    elif (root / "requirements.txt").is_file():
        targets.extend(["--requirement", "requirements.txt"])

    targets.extend(CHECK_PACKAGES[kind] for kind in sorted(kinds) if kind in CHECK_PACKAGES)
    if not targets:
        return ()

    return (
        Check(
            kind=CheckKind.INSTALL,
            name="venv",
            command=("python3", "-m", "venv", VENV_DIR),
            reason="isolate installed dependencies from the image",
        ),
        Check(
            kind=CheckKind.INSTALL,
            name="pip",
            command=("pip", "install", "--no-cache-dir", "--no-input", *targets),
            reason="install the project and the tools its checks need",
        ),
    )


def plan_checks(root: Path) -> ToolchainPlan:
    """Decide what to run against a repository, and record what was not run."""
    root = Path(root)
    pyproject = _read_pyproject(root)
    checks: list[Check] = []
    skipped: list[CheckResult] = []

    # Byte-compilation needs no dependencies and no configuration, so it is the
    # one check that always produces evidence. It is weak evidence — a file can
    # parse and still be nonsense — but a patch that breaks it is beyond doubt.
    checks.append(
        Check(
            kind=CheckKind.SYNTAX,
            name="compileall",
            command=("python3", "-m", "compileall", "-q", "-x", r"\.rewire-venv", "."),
            reason="always available; proves every file still parses",
        )
    )

    if has_tests(root):
        checks.append(
            Check(
                kind=CheckKind.TESTS,
                name="pytest",
                command=("python", "-m", "pytest", "-q", "-p", "no:cacheprovider"),
                reason="repository contains a test suite",
            )
        )
    else:
        skipped.append(
            CheckResult.skipped(CheckKind.TESTS, "pytest", "repository contains no tests to run")
        )

    ruff_configured = _tool_table(pyproject, "ruff") or any(
        (root / name).is_file() for name in ("ruff.toml", ".ruff.toml")
    )
    if ruff_configured:
        checks.append(
            Check(
                kind=CheckKind.LINT,
                name="ruff",
                command=("python", "-m", "ruff", "check", "--no-cache", "."),
                reason="repository configures ruff",
            )
        )
    else:
        skipped.append(
            CheckResult.skipped(CheckKind.LINT, "ruff", "repository does not configure ruff")
        )

    mypy_configured = _tool_table(pyproject, "mypy") or any(
        (root / name).is_file() for name in ("mypy.ini", ".mypy.ini")
    )
    if mypy_configured:
        checks.append(
            Check(
                kind=CheckKind.TYPECHECK,
                name="mypy",
                command=("python", "-m", "mypy", "--no-incremental", "."),
                reason="repository configures mypy",
            )
        )
    else:
        skipped.append(
            CheckResult.skipped(CheckKind.TYPECHECK, "mypy", "repository does not configure mypy")
        )

    ordered = tuple(checks)
    return ToolchainPlan(
        checks=ordered,
        install=detect_install(root, frozenset(check.kind for check in ordered)),
        skipped=tuple(skipped),
    )


__all__ = [
    "CHECK_PACKAGES",
    "CONTAINER_PATH",
    "TEST_DIRECTORIES",
    "VENV_DIR",
    "WORKSPACE_DIR",
    "Check",
    "ToolchainPlan",
    "detect_install",
    "has_tests",
    "plan_checks",
]
