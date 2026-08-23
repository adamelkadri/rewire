"""Propose, run, read the failure, try again.

This is the loop the whole project has been building towards. Phases 1 to 3 work
out what changed and where; Phase 4 writes a patch; Phase 5 executes it and
returns evidence. Here that evidence goes back to the agent, which is the first
point at which Rewire can improve on its own first guess rather than simply
reporting it.

Three properties are deliberate.

**Repair is driven by evidence, never by suspicion.** A retry happens only when
the sandbox actually ran the checks and found the patch broke something, or
could not apply it at all. An `INCONCLUSIVE` verdict -- no tests, tooling
missing, the suite already failing before the patch -- stops immediately,
because none of those describe a mistake the agent made and none of them would
be fixed by writing the patch differently. Retrying on "we learned nothing"
would burn a real budget chasing a problem the agent cannot reach.

**Each attempt starts from the original files.** There is no tool to un-stage an
edit, so a second attempt that inherited the first attempt's builder could only
add to a patch that was already wrong. Every attempt gets a fresh builder and is
asked for the complete migration; the previous diff is supplied as information,
not as a base.

**No attempt can be called successful without its own sandbox run.** The verdict
on the final patch comes from executing that patch, not from the fact that a
repair was attempted.

It lives under ``services`` rather than ``agents`` because it composes the agent
with the sandbox, and the sandbox already depends on ``rewire.agents.patch``.
See the package docstring.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rewire.agents.migration_agent import MigrationAgent, MigrationResult
from rewire.agents.patch import CandidatePatch
from rewire.agents.prompts import build_repair_prompt
from rewire.agents.workspace import Workspace
from rewire.analyzers.models import RepositoryIndex
from rewire.changes.models import ChangeReport
from rewire.core.logging import get_logger
from rewire.impact.models import ImpactReport
from rewire.sandbox.models import (
    CheckStatus,
    Verdict,
    VerificationReport,
    VerificationRequest,
)
from rewire.sandbox.verifier import verify

logger = get_logger(__name__)

#: Signature of the verification step, injected so the loop is testable without
#: Docker and swappable for a future backend.
VerifyCallable = Callable[..., VerificationReport]

#: Verdicts that describe a mistake in the patch, and are therefore worth
#: another attempt. Everything else means the sandbox learned nothing, which a
#: retry cannot change.
REPAIRABLE: frozenset[Verdict] = frozenset({Verdict.REGRESSED, Verdict.ERRORED})


@dataclass(frozen=True, slots=True)
class RepairPolicy:
    """Bounds on the repair loop.

    Separate from :class:`~rewire.agents.migration_agent.AgentBudget`, which
    bounds a single attempt. This bounds how many attempts there are and what
    they may cost in total, so that three attempts cannot each spend the
    per-attempt budget unnoticed.
    """

    #: Total attempts including the first. ``1`` disables repair entirely, which
    #: is what the Phase 10 ablation compares against.
    max_attempts: int = 3
    #: Ceiling across every attempt. Checked before starting one, so an attempt
    #: is never begun that the budget cannot pay for.
    max_total_tokens: int = 500_000


@dataclass(frozen=True, slots=True)
class Attempt:
    """One proposal and, if it produced a patch, the sandbox's judgement of it."""

    number: int
    result: MigrationResult
    #: ``None`` when the agent produced no patch, so there was nothing to run.
    report: VerificationReport | None = None

    @property
    def patch(self) -> CandidatePatch:
        """The patch this attempt proposed."""
        return self.result.patch

    @property
    def verified(self) -> bool:
        """Whether the sandbox confirmed this attempt's patch."""
        return self.report is not None and self.report.verified

    @property
    def verdict(self) -> Verdict | None:
        """The sandbox verdict, or ``None`` if nothing was run."""
        return self.report.verdict if self.report else None


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    """Every attempt made, and what finally became of the migration."""

    attempts: tuple[Attempt, ...] = ()
    #: Why the loop stopped. Always populated, including on success.
    stopped_because: str = ""
    duration_seconds: float = 0.0

    @property
    def final(self) -> Attempt | None:
        """The last attempt made, if any."""
        return self.attempts[-1] if self.attempts else None

    @property
    def verified(self) -> Attempt | None:
        """The attempt the sandbox confirmed, if one was confirmed."""
        return next((attempt for attempt in self.attempts if attempt.verified), None)

    @property
    def best(self) -> Attempt | None:
        """The attempt worth reporting: the verified one, else the last real patch.

        Not simply the last attempt. A final attempt can end without a patch at
        all -- the model refused, or the request failed -- and treating that as
        the result would discard a genuine candidate an earlier attempt
        produced, reporting "no patch" for a run that plainly made one.
        """
        if (verified := self.verified) is not None:
            return verified
        return next(
            (attempt for attempt in reversed(self.attempts) if not attempt.patch.is_empty), None
        )

    @property
    def patch(self) -> CandidatePatch:
        """The verified patch if there is one, else the best candidate proposed."""
        chosen = self.best
        return chosen.patch if chosen else CandidatePatch()

    @property
    def verdict(self) -> Verdict:
        """The verdict on the patch being reported."""
        chosen = self.best
        if chosen is None or chosen.report is None:
            return Verdict.INCONCLUSIVE
        return chosen.report.verdict

    @property
    def repaired(self) -> bool:
        """Whether repair is what produced the result.

        The metric this phase exists to make measurable: a run that failed
        verification and then passed it. Phase 10 compares this against the same
        agent with ``max_attempts=1``.
        """
        verified = self.verified
        return verified is not None and verified.number > 1

    @property
    def total_tokens(self) -> int:
        """Tokens spent across every attempt."""
        return sum(attempt.result.summary.usage.total_tokens for attempt in self.attempts)

    @property
    def total_cost_usd(self) -> float | None:
        """Cost across every attempt, or ``None`` if any attempt's cost is unknown."""
        total = 0.0
        for attempt in self.attempts:
            cost = attempt.result.summary.cost_usd
            if cost is None:
                return None
            total += cost
        return total


@dataclass(slots=True)
class _Ledger:
    """Bookkeeping the loop needs but the outcome should not carry."""

    seen_diffs: set[str] = field(default_factory=set)

    def is_repeat(self, patch: CandidatePatch) -> bool:
        """Whether this exact patch has already been proposed and rejected."""
        diff = patch.unified_diff()
        if diff in self.seen_diffs:
            return True
        self.seen_diffs.add(diff)
        return False


def _failures_for(report: VerificationReport) -> list[tuple[str, str]]:
    """Extract the output of the checks this patch actually broke.

    Only regressions are included. A check that was already failing at baseline
    is somebody else's bug, and handing it to the agent sends it off to fix code
    the migration never touched.
    """
    return [
        (result.name, result.outcome.output if result.outcome else "")
        for result in report.patched
        if result.kind in report.regressions
        and result.status in {CheckStatus.FAILED, CheckStatus.TIMED_OUT}
    ]


def migrate_with_repair(
    *,
    agent: MigrationAgent,
    repository: Path | str,
    workspace: Workspace,
    index: RepositoryIndex,
    changes: ChangeReport,
    impact: ImpactReport,
    request: VerificationRequest | None = None,
    policy: RepairPolicy | None = None,
    verifier: VerifyCallable | None = None,
    run_id: str | None = None,
) -> RepairOutcome:
    """Propose a patch, verify it, and retry with the failure until it holds.

    Args:
        agent: The migration agent. Reused across attempts; each attempt is a
            fresh run with a fresh patch builder.
        repository: The repository to verify against. Never modified.
        workspace: A read-only view of the same repository, for the agent.
        index: Its parsed symbol and reference index.
        changes: The detected API changes.
        impact: Those changes joined to the code they affect.
        request: Sandbox isolation and resource policy.
        policy: Bounds on the number of attempts and their total cost.
        verifier: The verification step. Resolved when the loop runs rather
            than bound as a default argument: a callable captured at
            definition time cannot be substituted, which would silently
            leave every test talking to a real Docker daemon.
        run_id: Base identifier; attempts are traced as ``<id>-1``, ``<id>-2``.

    Returns:
        Every attempt made, with the sandbox's verdict on each.
    """
    bounds = policy or RepairPolicy()
    run_checks = verifier or verify
    base = run_id or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    attempts: list[Attempt] = []
    ledger = _Ledger()
    feedback = ""
    stopped = ""

    def finish(reason: str) -> RepairOutcome:
        outcome = RepairOutcome(
            attempts=tuple(attempts),
            stopped_because=reason,
            duration_seconds=round(time.perf_counter() - started, 3),
        )
        logger.info(
            "repair_loop_finished",
            attempts=len(outcome.attempts),
            verdict=outcome.verdict.value,
            repaired=outcome.repaired,
            total_tokens=outcome.total_tokens,
            stopped_because=reason,
        )
        return outcome

    for number in range(1, bounds.max_attempts + 1):
        spent = sum(a.result.summary.usage.total_tokens for a in attempts)
        if spent >= bounds.max_total_tokens:
            return finish(f"token budget exhausted across attempts ({bounds.max_total_tokens})")

        result = agent.run(
            workspace=workspace,
            index=index,
            changes=changes,
            impact=impact,
            run_id=f"{base}-{number}",
            feedback=feedback,
        )

        if not result.summary.produced_patch or result.patch.is_empty:
            attempts.append(Attempt(number=number, result=result))
            return finish(f"attempt {number} produced no patch: {result.summary.outcome}")

        repeated = ledger.is_repeat(result.patch)
        report = run_checks(repository, result.patch, request=request)
        attempts.append(Attempt(number=number, result=result, report=report))
        _record_verification(agent, f"{base}-{number}", report)
        logger.info(
            "repair_attempt",
            attempt=number,
            verdict=report.verdict.value,
            files_changed=len(result.patch.files),
        )

        if report.verified:
            return finish("verified" if number == 1 else f"verified after {number} attempts")
        if report.verdict not in REPAIRABLE:
            return finish(f"{report.verdict.value} and not repairable: {report.reason}")
        if repeated:
            return finish(
                "the agent proposed a patch it had already proposed; retrying cannot help"
            )
        if number == bounds.max_attempts:
            stopped = (
                f"attempt budget exhausted ({bounds.max_attempts});"
                f" last verdict was {report.verdict.value}"
            )
            break

        feedback = build_repair_prompt(
            verdict=report.verdict.value,
            reason=report.reason,
            regressions=[kind.value for kind in report.regressions],
            failures=_failures_for(report),
            diff=result.patch.unified_diff(),
        )

    return finish(stopped or "no attempts were made")


def _record_verification(agent: MigrationAgent, run_id: str, report: VerificationReport) -> None:
    """Write the sandbox verdict beside the attempt's trace.

    Written as its own file rather than appended to the trace: the trace is
    closed when the agent's run ends, and reopening it to add an event that
    happened afterwards would put an event out of sequence in a log whose
    ordering Phases 8 to 10 rely on.
    """
    if agent.runs_dir is None:
        return
    directory = agent.runs_dir / run_id
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "verification.json").write_text(
            report.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - the trace directory already exists
        logger.warning("verification_not_written", run_id=run_id, error=str(exc))


__all__ = [
    "REPAIRABLE",
    "Attempt",
    "RepairOutcome",
    "RepairPolicy",
    "VerifyCallable",
    "migrate_with_repair",
]
