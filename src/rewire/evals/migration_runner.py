"""Running the migration benchmark, and reporting what it actually measured.

Two numbers come out of every arm, and the gap between them is the point.

**Verified rate** is how often Rewire said the patch works. **Correct rate** is
how often it did, judged by a contract test the agent never saw. A tool that
grades itself will always report the first; only the second is a claim about the
world, and their difference is the rate at which Rewire's own verification is
fooled.

The runner therefore never reports a success rate without also reporting the
overclaim rate beside it. A benchmark that hides its false positives is a
marketing document.

Each arm is the same dataset with a different repair budget, so "repair improves
success from X to Y" is a controlled comparison rather than two numbers from
different runs.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from rewire.agents.patch import CandidatePatch
from rewire.core.config import Settings
from rewire.core.logging import get_logger
from rewire.evals.migration_dataset import Expectation, MigrationCase
from rewire.llm.base import LLMProvider
from rewire.sandbox.models import Verdict, VerificationReport, VerificationRequest
from rewire.sandbox.verifier import verify
from rewire.services.migrate import MigrationRequest, MigrationStatus, run_migration
from rewire.services.repair import VerifyCallable

logger = get_logger(__name__)


class ArmConfig(BaseModel):
    """One experimental condition: the same dataset, a different repair budget."""

    model_config = ConfigDict(frozen=True)

    name: str
    max_attempts: int = Field(ge=1)
    description: str = ""


#: The controlled comparison this phase exists to make.
DEFAULT_ARMS: tuple[ArmConfig, ...] = (
    ArmConfig(
        name="no-repair",
        max_attempts=1,
        description="one attempt, no feedback from the sandbox",
    ),
    ArmConfig(
        name="repair",
        max_attempts=3,
        description="up to three attempts, each told why the last one failed",
    ),
)


class CaseOutcome(BaseModel):
    """What happened to one case in one arm."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    arm: str
    expectation: Expectation
    tags: tuple[str, ...] = ()

    #: What Rewire concluded, in its own words.
    status: MigrationStatus
    #: Whether Rewire claimed the patch works.
    claimed_verified: bool = False
    #: Whether the hidden contract test passed. ``None`` when it was not run --
    #: no patch to grade, or the case ships no hidden test.
    truly_correct: bool | None = None
    #: Why grading reached that answer, including when it could not run.
    grading_detail: str = ""

    files_changed: int = 0
    attempts: int = 0
    repaired: bool = False
    tokens: int = 0
    cost_usd: float | None = None
    duration_seconds: float = 0.0
    error: str = ""

    @property
    def succeeded(self) -> bool:
        """Whether this case was handled correctly, proven rather than claimed.

        For a migration case that means a patch Rewire verified *and* the hidden
        test accepted. For a no-op case it means Rewire left the repository
        alone: producing a confident patch for code that did not need one is the
        failure mode a no-op case exists to catch.
        """
        if self.expectation is Expectation.NO_OP:
            return self.status in {
                MigrationStatus.NO_BREAKING_CHANGES,
                MigrationStatus.NO_AFFECTED_CODE,
            }
        return self.claimed_verified and self.truly_correct is True

    @property
    def overclaimed(self) -> bool:
        """Rewire said the patch works and the hidden test says it does not."""
        return self.claimed_verified and self.truly_correct is False

    @property
    def underclaimed(self) -> bool:
        """The patch was correct but Rewire would not vouch for it."""
        return not self.claimed_verified and self.truly_correct is True


class ArmResult(BaseModel):
    """Aggregate results for one arm."""

    model_config = ConfigDict(frozen=True)

    arm: str
    description: str = ""
    max_attempts: int = 1
    outcomes: tuple[CaseOutcome, ...] = ()

    @property
    def total(self) -> int:
        """Cases attempted."""
        return len(self.outcomes)

    @property
    def succeeded(self) -> int:
        """Cases handled correctly."""
        return sum(1 for outcome in self.outcomes if outcome.succeeded)

    @property
    def success_rate(self) -> float:
        """Proven success rate. Zero cases is reported as zero, not as one."""
        return self.succeeded / self.total if self.total else 0.0

    @property
    def claimed(self) -> int:
        """Cases where Rewire said the patch works."""
        return sum(1 for outcome in self.outcomes if outcome.claimed_verified)

    @property
    def overclaimed(self) -> int:
        """Cases Rewire vouched for that the hidden test rejected."""
        return sum(1 for outcome in self.outcomes if outcome.overclaimed)

    @property
    def underclaimed(self) -> int:
        """Correct patches Rewire declined to vouch for."""
        return sum(1 for outcome in self.outcomes if outcome.underclaimed)

    @property
    def repaired(self) -> int:
        """Cases where a later attempt succeeded after an earlier one failed."""
        return sum(1 for outcome in self.outcomes if outcome.repaired)

    @property
    def errored(self) -> int:
        """Cases the harness could not evaluate at all."""
        return sum(1 for outcome in self.outcomes if outcome.error)

    @property
    def total_tokens(self) -> int:
        """Tokens spent across the arm."""
        return sum(outcome.tokens for outcome in self.outcomes)

    @property
    def total_cost_usd(self) -> float | None:
        """Cost across the arm, or ``None`` if any case's cost is unknown."""
        total = 0.0
        for outcome in self.outcomes:
            if outcome.cost_usd is None:
                return None
            total += outcome.cost_usd
        return total

    def by_tag(self) -> dict[str, tuple[int, int]]:
        """``tag -> (succeeded, total)``, so a headline can be broken apart."""
        counts: dict[str, tuple[int, int]] = {}
        for outcome in self.outcomes:
            for tag in outcome.tags:
                succeeded, total = counts.get(tag, (0, 0))
                counts[tag] = (succeeded + int(outcome.succeeded), total + 1)
        return dict(sorted(counts.items()))


class BenchmarkResult(BaseModel):
    """Every arm, and the configuration that produced them."""

    model_config = ConfigDict(frozen=True)

    arms: tuple[ArmResult, ...] = ()
    provider: str = ""
    model: str = ""
    dataset: str = ""
    cases: int = 0
    generated_at: str = ""
    duration_seconds: float = 0.0
    #: Cases in the dataset that ship no hidden test, and are therefore graded
    #: on Rewire's own word. Named so the number cannot hide.
    ungraded_cases: tuple[str, ...] = ()

    def arm(self, name: str) -> ArmResult | None:
        """One arm by name."""
        return next((arm for arm in self.arms if arm.arm == name), None)


@dataclass(slots=True)
class BenchmarkConfig:
    """How to run the benchmark."""

    dataset: Path
    arms: tuple[ArmConfig, ...] = DEFAULT_ARMS
    #: Run only these case identifiers, if given.
    only: tuple[str, ...] = ()
    #: Stop after this many cases per arm. Zero means no limit.
    limit: int = 0
    verification: VerificationRequest | None = None
    results_dir: Path = Path("evals/results")
    #: Written after every case, so a run killed half way is not lost.
    incremental: bool = True
    progress: Callable[[str], None] | None = field(default=None)


def _grade(
    case: MigrationCase,
    patch: CandidatePatch,
    *,
    verification: VerificationRequest | None,
    verifier: VerifyCallable | None,
) -> tuple[bool | None, str]:
    """Run the hidden contract test against a produced patch.

    Returns ``(passed, detail)``, with ``passed`` ``None`` when nothing could be
    graded. A case with no hidden test grades to ``None`` rather than to
    success: an ungraded case must not be able to inflate a success rate.
    """
    hidden = case.hidden_tests()
    if not hidden:
        return None, "case ships no hidden test, so its patch is ungraded"

    run_checks = verifier or verify
    report: VerificationReport = run_checks(
        case.repository, patch, request=verification, overlay=hidden
    )
    if report.verdict is Verdict.VERIFIED:
        return True, "the hidden contract test passed"
    return False, f"the hidden contract test did not pass: {report.reason}"


def evaluate_case(
    case: MigrationCase,
    arm: ArmConfig,
    *,
    provider: LLMProvider,
    settings: Settings,
    verification: VerificationRequest | None = None,
    verifier: VerifyCallable | None = None,
) -> CaseOutcome:
    """Run one case under one arm and grade the result.

    Harness failures are captured on the outcome rather than raised: one broken
    case must not discard the rest of a benchmark that costs real money to run.
    """
    started = time.perf_counter()
    base = CaseOutcome(
        case_id=case.case_id,
        arm=arm.name,
        expectation=case.expectation,
        tags=case.tags,
        status=MigrationStatus.NO_PATCH,
    )
    try:
        outcome = run_migration(
            MigrationRequest(
                repository=case.repository,
                old_spec=case.old_spec,
                new_spec=case.new_spec,
                packages=case.packages,
                apply=False,
                max_attempts=arm.max_attempts,
            ),
            provider=provider,
            settings=settings,
            verification=verification,
            verifier=verifier,
        )
    except Exception as exc:
        logger.warning("benchmark_case_failed", case=case.case_id, arm=arm.name, error=str(exc))
        return base.model_copy(
            update={
                "error": f"{type(exc).__name__}: {exc}",
                "duration_seconds": round(time.perf_counter() - started, 2),
            }
        )

    truly_correct: bool | None = None
    detail = "no patch was produced, so there was nothing to grade"
    if not outcome.patch.is_empty:
        try:
            truly_correct, detail = _grade(
                case, outcome.patch, verification=verification, verifier=verifier
            )
        except Exception as exc:
            detail = f"grading failed: {type(exc).__name__}: {exc}"

    repair = outcome.repair
    return base.model_copy(
        update={
            "status": outcome.status,
            "claimed_verified": outcome.verified,
            "truly_correct": truly_correct,
            "grading_detail": detail,
            "files_changed": len(outcome.patch.files),
            "attempts": len(repair.attempts) if repair else 0,
            "repaired": repair.repaired if repair else False,
            "tokens": repair.total_tokens if repair else 0,
            # No repair object means the pipeline stopped before calling a
            # model, so the case genuinely cost nothing. Reporting that as
            # "unknown" would poison the whole arm's total.
            "cost_usd": repair.total_cost_usd if repair else 0.0,
            "duration_seconds": round(time.perf_counter() - started, 2),
        }
    )


def run_benchmark(
    config: BenchmarkConfig,
    cases: Sequence[MigrationCase],
    *,
    provider: LLMProvider,
    settings: Settings,
    verifier: VerifyCallable | None = None,
) -> BenchmarkResult:
    """Run every arm over every case and aggregate the results."""
    selected = [case for case in cases if not config.only or case.case_id in config.only]
    if config.limit:
        selected = selected[: config.limit]

    started = time.perf_counter()
    arms: list[ArmResult] = []
    ungraded = tuple(case.case_id for case in selected if not case.hidden_tests())

    for arm in config.arms:
        outcomes: list[CaseOutcome] = []
        for index, case in enumerate(selected, start=1):
            if config.progress:
                config.progress(f"{arm.name} [{index}/{len(selected)}] {case.case_id}")
            outcomes.append(
                evaluate_case(
                    case,
                    arm,
                    provider=provider,
                    settings=settings,
                    verification=config.verification,
                    verifier=verifier,
                )
            )
            if config.incremental:
                _write_partial(config, arms, arm, outcomes, provider, len(selected))
        arms.append(
            ArmResult(
                arm=arm.name,
                description=arm.description,
                max_attempts=arm.max_attempts,
                outcomes=tuple(outcomes),
            )
        )

    return BenchmarkResult(
        arms=tuple(arms),
        provider=provider.name,
        model=provider.model,
        dataset=str(config.dataset),
        cases=len(selected),
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        duration_seconds=round(time.perf_counter() - started, 1),
        ungraded_cases=ungraded,
    )


def _write_partial(
    config: BenchmarkConfig,
    finished: list[ArmResult],
    current: ArmConfig,
    outcomes: list[CaseOutcome],
    provider: LLMProvider,
    total: int,
) -> None:
    """Persist progress after each case, so a killed run keeps what it paid for."""
    partial = BenchmarkResult(
        arms=(
            *finished,
            ArmResult(
                arm=current.name,
                description=current.description,
                max_attempts=current.max_attempts,
                outcomes=tuple(outcomes),
            ),
        ),
        provider=provider.name,
        model=provider.model,
        dataset=str(config.dataset),
        cases=total,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    try:
        config.results_dir.mkdir(parents=True, exist_ok=True)
        (config.results_dir / "migration-partial.json").write_text(
            partial.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - results dir is writable by then
        logger.warning("benchmark_partial_not_written", error=str(exc))


def render_markdown(result: BenchmarkResult) -> str:
    """Render the benchmark as a report a reader can argue with."""
    lines = [
        "# Migration benchmark",
        "",
        f"- dataset: `{result.dataset}` ({result.cases} case(s))",
        f"- model: `{result.provider}` / `{result.model}`",
        f"- generated: {result.generated_at}",
        f"- wall clock: {result.duration_seconds:.0f}s",
        "",
        "Every patch is graded by a contract test injected after the patch is applied",
        "and never present in the repository the agent could read. **Verified** is what",
        "Rewire claimed; **correct** is what the hidden test found. The gap between them",
        "is the rate at which Rewire's own verification was fooled.",
        "",
        "| Arm | Attempts | Correct | Verified | Overclaimed | Underclaimed |"
        " Repaired | Tokens | Cost |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for arm in result.arms:
        cost = f"${arm.total_cost_usd:.2f}" if arm.total_cost_usd is not None else "unknown"
        lines.append(
            f"| {arm.arm} | {arm.max_attempts} | "
            f"**{arm.succeeded}/{arm.total}** ({arm.success_rate:.0%}) | "
            f"{arm.claimed} | {arm.overclaimed} | {arm.underclaimed} | "
            f"{arm.repaired} | {arm.total_tokens} | {cost} |"
        )

    if len(result.arms) == 2:
        first, second = result.arms
        lines += [
            "",
            f"Repair moved the proven success rate from **{first.success_rate:.0%}** "
            f"({first.succeeded}/{first.total}) to **{second.success_rate:.0%}** "
            f"({second.succeeded}/{second.total}).",
        ]

    if result.ungraded_cases:
        lines += [
            "",
            f"**{len(result.ungraded_cases)} case(s) ship no hidden test** and are graded on "
            "Rewire's own word: " + ", ".join(f"`{name}`" for name in result.ungraded_cases) + ".",
        ]

    for arm in result.arms:
        lines += ["", f"## {arm.arm}", "", f"_{arm.description}_", ""]
        tags = arm.by_tag()
        if tags:
            lines += ["| Tag | Correct |", "|---|---|"]
            lines += [
                f"| {tag} | {succeeded}/{total} |" for tag, (succeeded, total) in tags.items()
            ]
            lines.append("")
        lines += [
            "| Case | Expect | Status | Verified | Correct | Attempts | Tokens | Note |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for outcome in arm.outcomes:
            correct = {True: "yes", False: "**no**", None: "-"}[outcome.truly_correct]
            note = outcome.error or outcome.grading_detail
            lines.append(
                f"| `{outcome.case_id}` | {outcome.expectation.value} | "
                f"{outcome.status.value} | {'yes' if outcome.claimed_verified else 'no'} | "
                f"{correct} | {outcome.attempts} | {outcome.tokens} | {note} |"
            )
    return "\n".join(lines) + "\n"


def write_results(result: BenchmarkResult, directory: Path | str) -> tuple[Path, Path]:
    """Write the benchmark as JSON and Markdown, returning both paths."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "migration.json"
    markdown_path = root / "migration.md"
    json_path.write_text(
        json.dumps(result.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, markdown_path


__all__ = [
    "DEFAULT_ARMS",
    "ArmConfig",
    "ArmResult",
    "BenchmarkConfig",
    "BenchmarkResult",
    "CaseOutcome",
    "evaluate_case",
    "render_markdown",
    "run_benchmark",
    "write_results",
]
