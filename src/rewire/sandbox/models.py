"""Typed results of sandboxed verification.

Every claim Rewire makes about a patch has to be traceable to a process that
actually ran: a command, an exit code, and its output. These types exist so
that no layer above them can assert success without carrying that evidence
along with it.

The distinction the type system enforces is between *not passing* and *not
having been checked*. A repository with no tests, a missing linter and a check
that timed out are three different things, and none of them is a pass. They are
represented as distinct statuses rather than collapsed into a boolean, because
collapsing them is exactly how a tool ends up reporting green for a repository
it never executed.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from rewire.analyzers.weakening import Weakening, withholds_verdict

#: Longest command output retained per check. Output is truncated in the middle
#: so that both the command's first complaint and its final summary survive.
MAX_OUTPUT_CHARS = 16_000


class CheckKind(StrEnum):
    """The class of evidence a check produces.

    Ordered from strongest to weakest: a passing test suite says the code still
    behaves, byte-compilation only says it parses.
    """

    TESTS = "tests"
    TYPECHECK = "typecheck"
    LINT = "lint"
    SYNTAX = "syntax"
    INSTALL = "install"

    @property
    def strength(self) -> int:
        """Rank by how much the check proves, strongest first.

        Reports are ordered by this rather than alphabetically, so the reader
        meets the test result before the linter's opinion.
        """
        return list(CheckKind).index(self)


class CheckStatus(StrEnum):
    """The outcome of one check.

    ``SKIPPED`` and ``UNAVAILABLE`` both mean "no evidence", and are kept apart
    because their fixes differ: the first is a property of the repository, the
    second of the sandbox image.
    """

    #: The command ran and exited zero.
    PASSED = "passed"
    #: The command ran and exited non-zero.
    FAILED = "failed"
    #: The command exceeded its wall-clock budget and was killed.
    TIMED_OUT = "timed_out"
    #: The tool is not installed in the sandbox, so nothing was measured.
    UNAVAILABLE = "unavailable"
    #: The repository does not configure this check, so it was never run.
    SKIPPED = "skipped"

    @property
    def is_evidence(self) -> bool:
        """Whether this status reflects a command that actually ran to completion."""
        return self in {CheckStatus.PASSED, CheckStatus.FAILED}


class Verdict(StrEnum):
    """The overall conclusion about a patch."""

    #: No check regressed and the test suite passed afterwards.
    VERIFIED = "verified"
    #: A check that passed before the patch does not pass after it.
    REGRESSED = "regressed"
    #: The suite passes, and it passes partly because the patch removed checks
    #: from it. Distinct from VERIFIED because the evidence is different: a suite
    #: that passes untouched establishes something a suite that passes after
    #: losing three assertions does not.
    WEAKENED = "weakened"
    #: Nothing regressed, but nothing established that the patch is correct.
    INCONCLUSIVE = "inconclusive"
    #: The patch could not be applied, or the sandbox failed to run it.
    ERRORED = "errored"

    @property
    def is_verified(self) -> bool:
        """Whether this verdict permits calling the patch verified."""
        return self is Verdict.VERIFIED


class CommandOutcome(BaseModel):
    """What happened when one command ran in the sandbox."""

    model_config = ConfigDict(frozen=True)

    command: tuple[str, ...]
    #: ``None`` only when the process was killed before it could exit.
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    #: Set when output was elided; the full output is never reconstructed.
    truncated: bool = False

    @property
    def succeeded(self) -> bool:
        """Whether the command exited zero."""
        return self.exit_code == 0

    @property
    def output(self) -> str:
        """Both streams together, in the order a terminal would show them."""
        return "\n".join(part for part in (self.stdout, self.stderr) if part)

    def tail(self, lines: int = 20) -> str:
        """The last ``lines`` lines of output, for a compact failure report."""
        return "\n".join(self.output.splitlines()[-lines:])


class CheckResult(BaseModel):
    """One check, its command, and the evidence it produced."""

    model_config = ConfigDict(frozen=True)

    kind: CheckKind
    #: Human-facing name of the tool, e.g. ``pytest``.
    name: str
    status: CheckStatus
    #: Absent when the check was skipped, since no command ever ran.
    outcome: CommandOutcome | None = None
    #: Why this check was selected, or why it was skipped.
    reason: str = ""

    @property
    def passed(self) -> bool:
        """Whether this check ran and passed."""
        return self.status is CheckStatus.PASSED

    @classmethod
    def skipped(cls, kind: CheckKind, name: str, reason: str) -> CheckResult:
        """Build a result for a check the repository does not configure."""
        return cls(kind=kind, name=name, status=CheckStatus.SKIPPED, reason=reason)


class VerificationReport(BaseModel):
    """The complete evidence for or against a candidate patch.

    Both a baseline and a patched run are recorded. Without the baseline a
    repository whose tests were already failing would be reported as broken by
    the agent, which is both wrong and the kind of wrongness that quietly
    destroys a benchmark's credibility.
    """

    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    #: Plain-language justification for the verdict. Always populated.
    reason: str
    baseline: tuple[CheckResult, ...] = ()
    patched: tuple[CheckResult, ...] = ()
    #: Checks that passed before the patch and no longer pass.
    regressions: tuple[CheckKind, ...] = ()
    #: Checks that were already failing before the patch. Not the agent's doing.
    pre_existing_failures: tuple[CheckKind, ...] = ()
    install: CheckResult | None = None
    image: str = ""
    files_changed: tuple[str, ...] = ()
    duration_seconds: float = 0.0
    #: Reductions the patch made to the repository's own tests. Present whatever
    #: the verdict, because a reviewer wants to see them on a patch that passed
    #: as much as on one that did not.
    weakenings: tuple[Weakening, ...] = ()

    @property
    def weakened(self) -> bool:
        """Whether the patch removed checks the suite previously made."""
        return withholds_verdict(self.weakenings)

    @property
    def verified(self) -> bool:
        """Whether the patch is backed by passing tests and no regressions."""
        return self.verdict.is_verified

    def check(self, kind: CheckKind, *, patched: bool = True) -> CheckResult | None:
        """Return one check from either run, if it is present."""
        source = self.patched if patched else self.baseline
        return next((result for result in source if result.kind is kind), None)


class VerificationRequest(BaseModel):
    """Inputs that determine how a verification run behaves."""

    model_config = ConfigDict(frozen=True)

    image: str = "python:3.12-slim"
    check_timeout_seconds: int = Field(default=600, gt=0)
    install_timeout_seconds: int = Field(default=900, gt=0)
    #: Installing a repository executes its build backend, so it is the one
    #: step permitted network access — and the only one.
    install: bool = True
    memory_limit_mb: int = Field(default=2048, ge=256)
    cpu_limit: float = Field(default=2.0, gt=0)
    pids_limit: int = Field(default=512, gt=0)
    read_only_rootfs: bool = True
    max_repo_size_mb: int = Field(default=512, gt=0)


def truncate_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    """Shorten output from the middle, returning the text and whether it was cut.

    The middle is dropped rather than the tail: a failing test run puts the
    error at the top and the summary at the bottom, and tail-only truncation
    throws away the first of those.
    """
    if len(text) <= limit:
        return text, False
    half = limit // 2
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n... [{dropped} characters elided] ...\n{text[-half:]}", True


__all__ = [
    "MAX_OUTPUT_CHARS",
    "CheckKind",
    "CheckResult",
    "CheckStatus",
    "CommandOutcome",
    "Verdict",
    "VerificationReport",
    "VerificationRequest",
    "truncate_output",
]
