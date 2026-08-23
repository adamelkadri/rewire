"""The whole pipeline behind one call, and the rules for writing to disk.

Phases 1 to 6 each produce something: a spec diff, an index, a ranked impact
report, a candidate patch, a verdict, a repaired patch. This composes them into
a single operation with a single answer, and it is the first place in Rewire
that is allowed to modify the user's repository.

Writing changes the risk profile completely, so it is governed by three refusals
rather than by a warning:

**An unverified patch is never written.** Not with a flag, not with a
confirmation prompt. The sandbox exists precisely so that "it looks right" is
not a reason to touch someone's code, and an override would make it one. A
patch that did not verify can still be printed, saved with ``--write-diff`` and
applied by hand — the user is not prevented from doing anything, only from
having Rewire do it on evidence Rewire does not have.

**Nothing is written into a dirty working tree** unless explicitly allowed.
Into a clean checkout, ``git diff`` shows exactly what Rewire did and
``git checkout`` undoes it. Into a tree with uncommitted edits, Rewire's change
and the user's become one indistinguishable diff.

**Nothing is written if the files moved.** The patch is checked against the
content it was proposed against immediately before writing, because the window
between verifying and writing is exactly where a repository can change.

The status enum matters as much as the refusals. "The upstream spec changed and
none of it affects this repository" is a *success* — it is what most runs will
report once Phase 12 watches specs automatically — and collapsing it into a
failure would make the common case look broken.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from rewire.agents.migration_agent import AgentBudget, MigrationAgent
from rewire.agents.patch import CandidatePatch, assert_patch_applies_to, write_patch
from rewire.agents.workspace import Workspace
from rewire.analyzers.index import build_index
from rewire.changes.differ import diff_specs
from rewire.changes.models import ChangeReport
from rewire.changes.spec import load_spec
from rewire.core.config import Settings
from rewire.core.errors import PatchError
from rewire.core.logging import get_logger
from rewire.gitio.repository import WorkingTree, inspect_working_tree
from rewire.impact.analyzer import analyse_impact
from rewire.impact.models import ImpactReport
from rewire.llm.base import LLMProvider
from rewire.sandbox.models import VerificationRequest
from rewire.services.repair import RepairOutcome, RepairPolicy, VerifyCallable, migrate_with_repair

logger = get_logger(__name__)


class MigrationStatus(StrEnum):
    """What a migration run concluded.

    Six outcomes rather than a boolean, because they call for different actions
    and three of them are not failures.
    """

    #: The two specifications differ in nothing that could break a caller.
    NO_BREAKING_CHANGES = "no_breaking_changes"
    #: There are breaking changes, but nothing in this repository uses them.
    NO_AFFECTED_CODE = "no_affected_code"
    #: A patch was verified and written to the working tree.
    APPLIED = "applied"
    #: A patch was verified. Nothing was written, because nothing asked it to be.
    VERIFIED = "verified"
    #: A patch was verified, but writing it was refused. See ``refusal``.
    REFUSED = "refused"
    #: A patch exists but the sandbox did not confirm it.
    UNVERIFIED = "unverified"
    #: The agent produced no patch at all.
    NO_PATCH = "no_patch"

    @property
    def is_success(self) -> bool:
        """Whether this outcome means the run did its job.

        "Nothing here is affected" is a success. Most runs will say it once
        Phase 12 watches upstream specifications, and treating it as a failure
        would make a healthy repository look broken every time an API moves.
        """
        return self in {
            MigrationStatus.NO_BREAKING_CHANGES,
            MigrationStatus.NO_AFFECTED_CODE,
            MigrationStatus.APPLIED,
            MigrationStatus.VERIFIED,
        }


@dataclass(frozen=True, slots=True)
class MigrationRequest:
    """What to migrate, and how far to go."""

    repository: Path
    old_spec: Path
    new_spec: Path
    #: Packages the API belongs to, to sharpen impact analysis.
    packages: tuple[str, ...] = ()
    #: Write the verified patch into the working tree.
    apply: bool = False
    #: Permit writing into a tree that already has uncommitted changes.
    allow_dirty: bool = False
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    """Everything the run established, and what it did about it."""

    run_id: str
    status: MigrationStatus
    changes: ChangeReport | None = None
    impact: ImpactReport | None = None
    repair: RepairOutcome | None = None
    #: Files written to the working tree. Empty unless the status is APPLIED.
    written: tuple[str, ...] = ()
    #: Why writing was refused, when it was. Empty otherwise.
    refusal: str = ""
    working_tree: WorkingTree | None = None
    duration_seconds: float = 0.0

    @property
    def patch(self) -> CandidatePatch:
        """The patch this run settled on, empty if there is none."""
        return self.repair.patch if self.repair else CandidatePatch()

    @property
    def verified(self) -> bool:
        """Whether a patch was confirmed by executing it."""
        return self.repair is not None and self.repair.verified is not None

    def summary_line(self) -> str:
        """One sentence describing the outcome, for a log or a notification."""
        match self.status:
            case MigrationStatus.NO_BREAKING_CHANGES:
                return "no breaking changes between the two specifications"
            case MigrationStatus.NO_AFFECTED_CODE:
                return "breaking changes found, but no code in this repository uses them"
            case MigrationStatus.APPLIED:
                return f"verified patch applied to {len(self.written)} file(s)"
            case MigrationStatus.VERIFIED:
                return f"patch verified across {len(self.patch.files)} file(s); nothing written"
            case MigrationStatus.REFUSED:
                return f"patch verified but not written: {self.refusal}"
            case MigrationStatus.UNVERIFIED:
                return "a patch was produced but could not be verified"
            case MigrationStatus.NO_PATCH:
                return "no patch was produced"


def run_migration(
    request: MigrationRequest,
    *,
    provider: LLMProvider,
    settings: Settings,
    verification: VerificationRequest | None = None,
    verifier: VerifyCallable | None = None,
    run_id: str | None = None,
) -> MigrationOutcome:
    """Run the whole pipeline and, if asked and permitted, write the result.

    Args:
        request: What to migrate and how far to go.
        provider: The model the agent will use.
        settings: Budgets, sandbox policy and where run artefacts are written.
        verification: Sandbox policy override.
        verifier: The verification step, injected for testing.
        run_id: Identifier for this run's artefacts; generated if absent.

    Raises:
        SpecParseError: Either specification could not be parsed.
        RepositoryError: The repository could not be read.
        ToolchainError: Docker is unavailable.
    """
    identifier = run_id or uuid.uuid4().hex[:12]
    started = time.perf_counter()

    def finish(
        status: MigrationStatus,
        *,
        changes: ChangeReport | None = None,
        impact: ImpactReport | None = None,
        repair: RepairOutcome | None = None,
        written: tuple[str, ...] = (),
        refusal: str = "",
        working_tree: WorkingTree | None = None,
    ) -> MigrationOutcome:
        outcome = MigrationOutcome(
            run_id=identifier,
            status=status,
            changes=changes,
            impact=impact,
            repair=repair,
            written=written,
            refusal=refusal,
            working_tree=working_tree,
            duration_seconds=round(time.perf_counter() - started, 3),
        )
        logger.info(
            "migration_finished",
            run_id=identifier,
            status=status.value,
            written=len(outcome.written),
        )
        _record(settings, outcome)
        return outcome

    changes = diff_specs(load_spec(request.old_spec), load_spec(request.new_spec))
    if changes.summary.breaking == 0 and changes.summary.potentially_breaking == 0:
        return finish(MigrationStatus.NO_BREAKING_CHANGES, changes=changes)

    index = build_index(request.repository)
    impact = analyse_impact(changes, index, packages=request.packages)
    if impact.summary.locations == 0:
        return finish(MigrationStatus.NO_AFFECTED_CODE, changes=changes, impact=impact)

    # Check the write precondition before spending anything on a model. Asking
    # for --apply into a dirty tree is a refusal either way; discovering that
    # after an agent run and two container runs costs real money for an answer
    # that was available in milliseconds.
    tree = inspect_working_tree(request.repository) if request.apply else None
    if tree is not None and not tree.is_clean and not request.allow_dirty:
        return finish(
            MigrationStatus.REFUSED,
            changes=changes,
            impact=impact,
            refusal=tree.describe(),
            working_tree=tree,
        )

    settings.ensure_data_dirs()
    agent = MigrationAgent(
        provider,
        budget=AgentBudget(
            max_tokens=settings.agent.max_tokens_per_task,
            max_files=settings.agent.max_files_per_patch,
            max_output_tokens=settings.llm.max_output_tokens,
        ),
        runs_dir=settings.runs_dir,
    )
    repair = migrate_with_repair(
        agent=agent,
        repository=request.repository,
        workspace=Workspace.open(request.repository),
        index=index,
        changes=changes,
        impact=impact,
        request=verification or _sandbox_request(settings),
        policy=RepairPolicy(
            max_attempts=request.max_attempts,
            max_total_tokens=settings.agent.max_tokens_per_task * max(request.max_attempts, 1),
        ),
        verifier=verifier,
        run_id=identifier,
    )

    if repair.patch.is_empty:
        return finish(MigrationStatus.NO_PATCH, changes=changes, impact=impact, repair=repair)
    if repair.verified is None:
        return finish(MigrationStatus.UNVERIFIED, changes=changes, impact=impact, repair=repair)
    if not request.apply:
        return finish(MigrationStatus.VERIFIED, changes=changes, impact=impact, repair=repair)

    try:
        assert_patch_applies_to(repair.patch, request.repository)
        written = write_patch(repair.patch, request.repository)
    except PatchError as exc:
        return finish(
            MigrationStatus.REFUSED,
            changes=changes,
            impact=impact,
            repair=repair,
            refusal=str(exc),
            working_tree=tree,
        )

    return finish(
        MigrationStatus.APPLIED,
        changes=changes,
        impact=impact,
        repair=repair,
        written=tuple(written),
        working_tree=tree,
    )


def _sandbox_request(settings: Settings) -> VerificationRequest:
    """Translate the sandbox settings block into a verification policy."""
    sandbox = settings.sandbox
    return VerificationRequest(
        image=sandbox.image,
        check_timeout_seconds=sandbox.timeout_seconds,
        memory_limit_mb=sandbox.memory_limit_mb,
        cpu_limit=sandbox.cpu_limit,
        pids_limit=sandbox.pids_limit,
        read_only_rootfs=sandbox.read_only_rootfs,
        max_repo_size_mb=sandbox.max_repo_size_mb,
    )


def _record(settings: Settings, outcome: MigrationOutcome) -> None:
    """Write a machine-readable record of the run beside its traces.

    Phase 8 aggregates these into a success rate, so it is written for every
    terminal status including the ones where nothing happened -- a dataset of
    only the interesting runs is a dataset with a hole in it.
    """
    directory = settings.runs_dir / outcome.run_id
    payload = {
        "run_id": outcome.run_id,
        "status": outcome.status.value,
        "summary": outcome.summary_line(),
        "duration_seconds": outcome.duration_seconds,
        "files_written": list(outcome.written),
        "refusal": outcome.refusal,
        "changes": outcome.changes.summary.model_dump(mode="json") if outcome.changes else None,
        "affected_locations": outcome.impact.summary.locations if outcome.impact else 0,
        "attempts": [
            {
                "number": attempt.number,
                "verdict": attempt.verdict.value if attempt.verdict else None,
                "files": len(attempt.patch.files),
                "tokens": attempt.result.summary.usage.total_tokens,
            }
            for attempt in (outcome.repair.attempts if outcome.repair else ())
        ],
        "repaired": outcome.repair.repaired if outcome.repair else False,
        "total_tokens": outcome.repair.total_tokens if outcome.repair else 0,
        "total_cost_usd": outcome.repair.total_cost_usd if outcome.repair else None,
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "migration.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - the run directory is already writable
        logger.warning("migration_record_not_written", run_id=outcome.run_id, error=str(exc))


__all__ = [
    "MigrationOutcome",
    "MigrationRequest",
    "MigrationStatus",
    "run_migration",
]
