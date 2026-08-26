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

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rewire.agents.config import DEFAULT_AGENT_CONFIG, AgentConfig
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
from rewire.services.record import MigrationStatus, write_record
from rewire.services.repair import RepairOutcome, RepairPolicy, VerifyCallable, migrate_with_repair

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MigrationTask:
    """*What* to migrate.

    Every field here is a description of the job, and a caller is entitled to
    choose all of them. This is the half an HTTP request body may fill in.
    """

    repository: Path
    old_spec: Path
    new_spec: Path
    #: Packages the API belongs to, to sharpen impact analysis.
    packages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MigrationPolicy:
    """*How far* a run may go.

    Every field here is an authority rather than a preference: permission to
    write, permission to write over uncommitted work, how much money the run may
    spend. Separated from :class:`MigrationTask` so that the type system says
    where each value is allowed to come from — the task from whoever asked, the
    policy from whoever runs the server. A request body that could set ``apply``
    would be a request body that can write to disk.
    """

    #: Write the verified patch into the working tree.
    apply: bool = False
    #: Permit writing into a tree that already has uncommitted changes.
    allow_dirty: bool = False
    max_attempts: int = 3
    #: What the agent is given. Varied only by the ablation benchmark; every
    #: other caller gets the shipped configuration.
    agent: AgentConfig = DEFAULT_AGENT_CONFIG
    #: Stop when impact analysis finds nothing. True in the product: calling a
    #: model about a repository with no affected code wastes money to reach the
    #: answer that was already available. An ablation measuring what impact
    #: analysis is worth has to be able to switch this off, because "it can tell
    #: you there is nothing to do" is part of what it is worth.
    require_affected_code: bool = True

    @classmethod
    def read_only(cls, *, max_attempts: int = 3) -> MigrationPolicy:
        """A policy that cannot write, for a caller that is not at the keyboard.

        Named rather than assembled at each call site so that "this run may not
        touch the working tree" is one reviewable thing instead of two booleans
        someone has to notice are both false.
        """
        return cls(apply=False, allow_dirty=False, max_attempts=max_attempts)


@dataclass(frozen=True, slots=True)
class MigrationRequest:
    """A task and the authority to carry it out.

    The two are carried separately and read through this one object, so that
    every existing caller keeps reading ``request.repository`` and
    ``request.apply`` while construction has to say which half each value came
    from.
    """

    task: MigrationTask
    policy: MigrationPolicy = MigrationPolicy()

    @property
    def repository(self) -> Path:
        """Repository to migrate."""
        return self.task.repository

    @property
    def old_spec(self) -> Path:
        """Specification the code currently targets."""
        return self.task.old_spec

    @property
    def new_spec(self) -> Path:
        """Specification to migrate to."""
        return self.task.new_spec

    @property
    def packages(self) -> tuple[str, ...]:
        """Packages the API belongs to."""
        return self.task.packages

    @property
    def apply(self) -> bool:
        """Whether a verified patch may be written to the working tree."""
        return self.policy.apply

    @property
    def allow_dirty(self) -> bool:
        """Whether writing into a tree with uncommitted changes is permitted."""
        return self.policy.allow_dirty

    @property
    def max_attempts(self) -> int:
        """Repair attempts allowed."""
        return self.policy.max_attempts

    @property
    def agent(self) -> AgentConfig:
        """What the agent is given."""
        return self.policy.agent

    @property
    def require_affected_code(self) -> bool:
        """Whether a run stops when impact analysis finds nothing."""
        return self.policy.require_affected_code


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


#: Builds the agent for one run. Takes the configuration because the ablation
#: benchmark varies it per run; every other caller passes the shipped one.
AgentFactory = Callable[[AgentConfig], MigrationAgent]


@dataclass(frozen=True, slots=True)
class MigrationRuntime:
    """Everything a run needs that is not the request.

    ``run_migration`` used to take :class:`Settings` and wire itself up: build a
    provider's agent, translate the settings block into a sandbox policy, decide
    where artefacts go. That is the right shape for a command-line tool, which
    starts, does one migration and exits. It is the wrong shape for a server,
    which wants to build the expensive parts once and reuse them across jobs, and
    which should not be reading a process-wide settings singleton per request.

    Holding the wiring in one value also means a caller can replace exactly one
    piece — a sandbox policy, the verifier — without reimplementing the rest.
    """

    build_agent: AgentFactory
    #: Sandbox policy for every verification in the run.
    verification: VerificationRequest
    #: Ceiling on tokens for a single attempt. The repair budget is a multiple.
    max_tokens_per_attempt: int
    #: Where run artefacts are written.
    runs_dir: Path
    #: The verification step. ``None`` uses the real sandbox.
    verifier: VerifyCallable | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        provider: LLMProvider,
        verification: VerificationRequest | None = None,
        verifier: VerifyCallable | None = None,
    ) -> MigrationRuntime:
        """Wire a runtime the way the command-line tool does.

        The one place that reads :class:`Settings`, so everything downstream
        takes values rather than a configuration object.
        """
        settings.ensure_data_dirs()
        budget = AgentBudget(
            max_tokens=settings.agent.max_tokens_per_task,
            max_files=settings.agent.max_files_per_patch,
            max_output_tokens=settings.llm.max_output_tokens,
        )
        runs_dir = settings.runs_dir

        def build_agent(config: AgentConfig) -> MigrationAgent:
            return MigrationAgent(provider, budget=budget, runs_dir=runs_dir, config=config)

        return cls(
            build_agent=build_agent,
            verification=verification or _sandbox_request(settings),
            max_tokens_per_attempt=settings.agent.max_tokens_per_task,
            runs_dir=runs_dir,
            verifier=verifier,
        )


def run_migration(
    request: MigrationRequest,
    *,
    runtime: MigrationRuntime,
    run_id: str | None = None,
) -> MigrationOutcome:
    """Run the whole pipeline and, if asked and permitted, write the result.

    Args:
        request: What to migrate and how far to go.
        runtime: The wiring — how to build the agent, the sandbox policy, where
            artefacts go. Built once and reused by a server; built per command by
            the CLI.
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
        write_record(runtime.runs_dir, outcome)
        return outcome

    changes = diff_specs(load_spec(request.old_spec), load_spec(request.new_spec))
    if changes.summary.breaking == 0 and changes.summary.potentially_breaking == 0:
        return finish(MigrationStatus.NO_BREAKING_CHANGES, changes=changes)

    index = build_index(request.repository)
    impact = analyse_impact(changes, index, packages=request.packages)
    if impact.summary.locations == 0 and request.require_affected_code:
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

    repair = migrate_with_repair(
        agent=runtime.build_agent(request.agent),
        repository=request.repository,
        workspace=Workspace.open(request.repository),
        index=index,
        changes=changes,
        impact=impact,
        request=runtime.verification,
        policy=RepairPolicy(
            max_attempts=request.max_attempts,
            max_total_tokens=runtime.max_tokens_per_attempt * max(request.max_attempts, 1),
        ),
        verifier=runtime.verifier,
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


__all__ = [
    "MigrationOutcome",
    "MigrationPolicy",
    "MigrationRequest",
    "MigrationRuntime",
    "MigrationStatus",
    "MigrationTask",
    "run_migration",
]
