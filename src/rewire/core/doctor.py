"""Environment preflight checks powering ``rewire doctor``.

Rewire depends on external tooling it cannot vendor: Git for branch/diff
handling, Docker for sandboxed verification, and ripgrep as a fallback search
backend. ``doctor`` probes each dependency for real -- it runs the binary and
reads its output rather than merely testing for the file's existence -- so that
a green report is evidence the toolchain works, not that it is installed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, Field

from rewire.core.config import Settings

#: Lowest Python version Rewire supports.
MINIMUM_PYTHON: tuple[int, int] = (3, 12)

#: Per-probe wall-clock budget. Docker in particular can hang when the daemon
#: is unreachable, so every probe is bounded.
PROBE_TIMEOUT_SECONDS = 10.0


class CheckStatus(StrEnum):
    """Outcome of a single preflight check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


class CheckResult(BaseModel):
    """The result of one preflight check."""

    name: str
    status: CheckStatus
    detail: str
    remedy: str | None = None

    @property
    def is_blocking(self) -> bool:
        """Whether this result should make ``doctor`` exit non-zero."""
        return self.status is CheckStatus.FAIL


class DoctorReport(BaseModel):
    """The full set of preflight results."""

    results: list[CheckResult] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no check failed. Warnings do not block."""
        return not any(result.is_blocking for result in self.results)

    def counts(self) -> dict[CheckStatus, int]:
        """Return the number of results per status."""
        return {
            status: sum(1 for result in self.results if result.status is status)
            for status in CheckStatus
        }


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a fixed command list and capture its output.

    The command is always a literal argument list built in this module -- never
    a shell string and never user input -- so there is no injection surface.
    """
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        list(command),
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
    )


def _probe(
    name: str,
    command: Sequence[str],
    *,
    remedy: str,
    required: bool,
) -> CheckResult:
    """Probe an external binary by executing it, and classify the outcome."""
    failure_status = CheckStatus.FAIL if required else CheckStatus.WARN
    executable = shutil.which(command[0])
    if executable is None:
        return CheckResult(
            name=name,
            status=failure_status,
            detail=f"{command[0]!r} was not found on PATH",
            remedy=remedy,
        )

    try:
        completed = _run(command)
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=name,
            status=failure_status,
            detail=f"{' '.join(command)!r} timed out after {PROBE_TIMEOUT_SECONDS:.0f}s",
            remedy=remedy,
        )
    except OSError as exc:
        return CheckResult(
            name=name,
            status=failure_status,
            detail=f"{command[0]!r} could not be executed: {exc}",
            remedy=remedy,
        )

    if completed.returncode != 0:
        stderr = completed.stderr.strip().splitlines()
        reason = stderr[0] if stderr else f"exit code {completed.returncode}"
        return CheckResult(name=name, status=failure_status, detail=reason, remedy=remedy)

    stdout = completed.stdout.strip()
    return CheckResult(
        name=name,
        status=CheckStatus.OK,
        detail=stdout.splitlines()[0] if stdout else "available",
    )


def check_python_version() -> CheckResult:
    """Check that the interpreter meets the minimum supported version."""
    current = sys.version_info[:2]
    rendered = f"{current[0]}.{current[1]}"
    required = f"{MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}"
    if current < MINIMUM_PYTHON:
        return CheckResult(
            name="python",
            status=CheckStatus.FAIL,
            detail=f"Python {rendered} is older than the required {required}",
            remedy=f"Install Python {required} or newer and recreate the virtualenv",
        )
    return CheckResult(
        name="python", status=CheckStatus.OK, detail=f"Python {sys.version.split()[0]}"
    )


def check_git() -> CheckResult:
    """Check that Git is installed and executable."""
    return _probe(
        "git",
        ["git", "--version"],
        remedy="Install Git (https://git-scm.com/downloads)",
        required=True,
    )


def check_docker() -> CheckResult:
    """Check that the Docker daemon is reachable, not merely that the CLI exists."""
    return _probe(
        "docker",
        ["docker", "version", "--format", "{{.Server.Version}}"],
        remedy="Install Docker and start the daemon; sandboxed verification requires it",
        required=True,
    )


def check_ripgrep() -> CheckResult:
    """Check for ripgrep, the fallback search backend (AST analysis is primary)."""
    return _probe(
        "ripgrep",
        ["rg", "--version"],
        remedy="Install ripgrep (brew install ripgrep) to enable fallback text search",
        required=False,
    )


def check_llm_credentials(settings: Settings) -> CheckResult:
    """Check that the configured LLM provider has a credential available."""
    provider = settings.llm.provider
    if provider == "null":
        return CheckResult(
            name="llm",
            status=CheckStatus.WARN,
            detail="No LLM provider configured; deterministic analysis only",
            remedy="Set REWIRE_LLM__PROVIDER and the matching API key to enable the agent",
        )

    key_field = f"{provider}_api_key"
    secret = getattr(settings.llm, key_field, None)
    if secret is None or not secret.get_secret_value():
        return CheckResult(
            name="llm",
            status=CheckStatus.FAIL,
            detail=f"Provider {provider!r} is selected but no API key is set",
            remedy=f"Set REWIRE_LLM__{key_field.upper()} in your environment or .env file",
        )
    return CheckResult(
        name="llm",
        status=CheckStatus.OK,
        detail=f"Provider {provider!r} with model {settings.llm.model!r}",
    )


def check_data_dir(settings: Settings) -> CheckResult:
    """Check that the configured data directory can be created and written to."""
    directory = settings.data_dir
    try:
        settings.ensure_data_dirs()
        probe = directory / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            name="data-dir",
            status=CheckStatus.FAIL,
            detail=f"{directory} is not writable: {exc}",
            remedy="Point REWIRE_DATA_DIR at a writable location",
        )
    return CheckResult(name="data-dir", status=CheckStatus.OK, detail=f"{directory} is writable")


def run_checks(settings: Settings) -> DoctorReport:
    """Run every preflight check and return the combined report."""
    return DoctorReport(
        results=[
            check_python_version(),
            check_git(),
            check_docker(),
            check_ripgrep(),
            check_llm_credentials(settings),
            check_data_dir(settings),
        ]
    )


__all__ = [
    "MINIMUM_PYTHON",
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "check_data_dir",
    "check_docker",
    "check_git",
    "check_llm_credentials",
    "check_python_version",
    "check_ripgrep",
    "run_checks",
]
