"""Tests for the benchmark harness: grading, arms, aggregation and reporting.

Both the model and the sandbox are scripted. What is being tested is the
arithmetic and the honesty of the report — that a case with no hidden test
cannot count as a success, that an overclaim is counted as a failure, and that a
broken case does not discard a benchmark that costs real money to run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rewire.core.config import Settings
from rewire.evals.migration_dataset import Expectation, MigrationCase
from rewire.evals.migration_runner import (
    DEFAULT_ARMS,
    ArmConfig,
    ArmResult,
    BenchmarkConfig,
    BenchmarkResult,
    CaseOutcome,
    evaluate_case,
    render_markdown,
    run_benchmark,
    write_results,
)
from rewire.llm import ScriptBuilder, ScriptedProvider
from rewire.sandbox.models import (
    CheckKind,
    CheckResult,
    CheckStatus,
    CommandOutcome,
    Verdict,
    VerificationReport,
)
from rewire.services.migrate import MigrationStatus

SPEC = (
    'openapi: "3.0.3"\ninfo: {{title: API, version: "{v}"}}\n'
    + """paths:
  /v1/chat/completions:
    post:
      requestBody:
        content:
          application/json:
            schema: {{type: object, properties: {{{field}: {{type: integer}}}}}}
      responses: {{'200': {{description: OK}}}}
"""
)

CLIENT = """from openai import OpenAI

client = OpenAI()


def ask():
    return client.chat.completions.create(max_tokens=100)
"""


@pytest.fixture
def case(tmp_path: Path) -> MigrationCase:
    directory = tmp_path / "01-case"
    repo = directory / "repo"
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nname="a"\nversion="1"\ndependencies=["openai"]\n', encoding="utf-8"
    )
    (repo / "app.py").write_text(CLIENT, encoding="utf-8")
    (directory / "old.yaml").write_text(SPEC.format(v="1", field="max_tokens"), encoding="utf-8")
    (directory / "new.yaml").write_text(
        SPEC.format(v="2", field="max_completion_tokens"), encoding="utf-8"
    )
    hidden = directory / "hidden" / "tests"
    hidden.mkdir(parents=True)
    (hidden / "test_contract.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    return MigrationCase(
        case_id="01-case",
        description="a rename",
        expectation=Expectation.MIGRATE,
        tags=("change:field-renamed",),
        rationale="the old key is gone",
        directory=directory,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / ".rewire")


def provider() -> ScriptedProvider:
    builder = ScriptBuilder()
    for _ in range(4):
        builder = builder.calls(
            "propose_edit",
            file="app.py",
            old_text="max_tokens=100",
            new_text="max_completion_tokens=100",
            rationale="renamed",
        ).says("Renamed.")
    return builder.build()


def report_for(verdict: Verdict) -> VerificationReport:
    passed = verdict is Verdict.VERIFIED
    tests = CheckResult(
        kind=CheckKind.TESTS,
        name="pytest",
        status=CheckStatus.PASSED if passed else CheckStatus.FAILED,
        outcome=CommandOutcome(command=("pytest",), exit_code=0 if passed else 1),
        reason="",
    )
    return VerificationReport(
        verdict=verdict, reason=f"scripted {verdict.value}", baseline=(tests,), patched=(tests,)
    )


def verifier(*, migration: Verdict, grading: Verdict):
    """Answer differently for the migration run and for the hidden-test grading."""

    def verify(*_args: object, **kwargs: object) -> VerificationReport:
        return report_for(grading if kwargs.get("overlay") else migration)

    return verify


# ------------------------------------------------------------------ grading ---


def test_a_verified_and_correct_patch_succeeds(case: MigrationCase, settings: Settings) -> None:
    outcome = evaluate_case(
        case,
        DEFAULT_ARMS[1],
        provider=provider(),
        settings=settings,
        verifier=verifier(migration=Verdict.VERIFIED, grading=Verdict.VERIFIED),
    )
    assert outcome.claimed_verified
    assert outcome.truly_correct is True
    assert outcome.succeeded
    assert not outcome.overclaimed


def test_a_verified_but_wrong_patch_is_an_overclaim(
    case: MigrationCase, settings: Settings
) -> None:
    """The dangerous outcome: Rewire vouched for a patch the contract rejects."""
    outcome = evaluate_case(
        case,
        DEFAULT_ARMS[1],
        provider=provider(),
        settings=settings,
        verifier=verifier(migration=Verdict.VERIFIED, grading=Verdict.REGRESSED),
    )
    assert outcome.claimed_verified
    assert outcome.truly_correct is False
    assert outcome.overclaimed
    assert not outcome.succeeded


def test_a_correct_patch_rewire_would_not_vouch_for_is_an_underclaim(
    case: MigrationCase, settings: Settings
) -> None:
    outcome = evaluate_case(
        case,
        DEFAULT_ARMS[0],
        provider=provider(),
        settings=settings,
        verifier=verifier(migration=Verdict.REGRESSED, grading=Verdict.VERIFIED),
    )
    assert outcome.underclaimed
    assert not outcome.succeeded


def test_a_case_without_hidden_tests_cannot_succeed(
    case: MigrationCase, settings: Settings, tmp_path: Path
) -> None:
    """An ungraded case must never be able to inflate a success rate."""
    import shutil

    shutil.rmtree(case.directory / "hidden")
    outcome = evaluate_case(
        case,
        DEFAULT_ARMS[1],
        provider=provider(),
        settings=settings,
        verifier=verifier(migration=Verdict.VERIFIED, grading=Verdict.VERIFIED),
    )
    assert outcome.claimed_verified
    assert outcome.truly_correct is None
    assert not outcome.succeeded
    assert "ships no hidden test" in outcome.grading_detail


def test_a_no_op_case_succeeds_by_doing_nothing(case: MigrationCase, settings: Settings) -> None:
    """Producing a confident patch for code that needs none is the failure here."""
    outcome = CaseOutcome(
        case_id="x",
        arm="repair",
        expectation=Expectation.NO_OP,
        status=MigrationStatus.NO_AFFECTED_CODE,
    )
    assert outcome.succeeded
    assert not CaseOutcome(
        case_id="x", arm="repair", expectation=Expectation.NO_OP, status=MigrationStatus.VERIFIED
    ).succeeded


def test_a_harness_failure_is_recorded_not_raised(case: MigrationCase, settings: Settings) -> None:
    """One broken case must not discard a benchmark that cost real money."""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the daemon went away")

    outcome = evaluate_case(
        case, DEFAULT_ARMS[1], provider=provider(), settings=settings, verifier=explode
    )
    assert "RuntimeError" in outcome.error
    assert not outcome.succeeded


def test_a_grading_failure_is_recorded_separately(case: MigrationCase, settings: Settings) -> None:
    calls = {"n": 0}

    def verify(*_args: object, **kwargs: object) -> VerificationReport:
        if kwargs.get("overlay"):
            raise RuntimeError("grading blew up")
        calls["n"] += 1
        return report_for(Verdict.VERIFIED)

    outcome = evaluate_case(
        case, DEFAULT_ARMS[1], provider=provider(), settings=settings, verifier=verify
    )
    assert outcome.claimed_verified
    assert outcome.truly_correct is None
    assert "grading failed" in outcome.grading_detail
    assert not outcome.error


# ------------------------------------------------------------------- arms ---


def test_both_arms_run_over_the_same_cases(case: MigrationCase, settings: Settings) -> None:
    result = run_benchmark(
        BenchmarkConfig(dataset=Path("d"), results_dir=settings.data_dir, incremental=False),
        [case],
        provider=provider(),
        settings=settings,
        verifier=verifier(migration=Verdict.VERIFIED, grading=Verdict.VERIFIED),
    )
    assert [arm.arm for arm in result.arms] == ["no-repair", "repair"]
    assert all(arm.total == 1 for arm in result.arms)
    assert result.arm("repair") is not None
    assert result.arm("absent") is None


def test_the_arms_differ_only_in_attempt_budget() -> None:
    assert DEFAULT_ARMS[0].max_attempts == 1
    assert DEFAULT_ARMS[1].max_attempts > 1


def test_cases_can_be_filtered_and_limited(case: MigrationCase, settings: Settings) -> None:
    config = BenchmarkConfig(
        dataset=Path("d"),
        arms=(ArmConfig(name="solo", max_attempts=1),),
        only=("absent",),
        results_dir=settings.data_dir,
        incremental=False,
    )
    result = run_benchmark(
        config,
        [case],
        provider=provider(),
        settings=settings,
        verifier=verifier(migration=Verdict.VERIFIED, grading=Verdict.VERIFIED),
    )
    assert result.arms[0].total == 0
    assert result.arms[0].success_rate == 0.0


def test_progress_is_reported(case: MigrationCase, settings: Settings) -> None:
    seen: list[str] = []
    run_benchmark(
        BenchmarkConfig(
            dataset=Path("d"),
            arms=(ArmConfig(name="solo", max_attempts=1),),
            results_dir=settings.data_dir,
            incremental=False,
            progress=seen.append,
        ),
        [case],
        provider=provider(),
        settings=settings,
        verifier=verifier(migration=Verdict.VERIFIED, grading=Verdict.VERIFIED),
    )
    assert seen == ["solo [1/1] 01-case"]


def test_progress_is_written_after_every_case(case: MigrationCase, settings: Settings) -> None:
    """A run killed half way must keep what it already paid for."""
    results = settings.data_dir / "results"
    run_benchmark(
        BenchmarkConfig(
            dataset=Path("d"),
            arms=(ArmConfig(name="solo", max_attempts=1),),
            results_dir=results,
        ),
        [case],
        provider=provider(),
        settings=settings,
        verifier=verifier(migration=Verdict.VERIFIED, grading=Verdict.VERIFIED),
    )
    partial = json.loads((results / "migration-partial.json").read_text(encoding="utf-8"))
    assert partial["arms"][0]["outcomes"][0]["case_id"] == "01-case"


# -------------------------------------------------------------- aggregation ---


def outcome(**kwargs: object) -> CaseOutcome:
    base = {
        "case_id": "c",
        "arm": "a",
        "expectation": Expectation.MIGRATE,
        "status": MigrationStatus.VERIFIED,
    }
    return CaseOutcome(**{**base, **kwargs})  # type: ignore[arg-type]


def test_rates_are_computed_over_attempted_cases() -> None:
    arm = ArmResult(
        arm="a",
        outcomes=(
            outcome(claimed_verified=True, truly_correct=True, tags=("x",)),
            outcome(claimed_verified=True, truly_correct=False, tags=("x",)),
            outcome(claimed_verified=False, truly_correct=None, tags=("y",)),
        ),
    )
    assert (arm.succeeded, arm.total) == (1, 3)
    assert arm.success_rate == pytest.approx(1 / 3)
    assert arm.claimed == 2
    assert arm.overclaimed == 1
    assert arm.by_tag() == {"x": (1, 2), "y": (0, 1)}


def test_an_empty_arm_reports_zero_not_one() -> None:
    """A benchmark that ran nothing has not succeeded at everything."""
    assert ArmResult(arm="a").success_rate == 0.0


def test_cost_is_unknown_if_any_case_is_unpriced() -> None:
    arm = ArmResult(arm="a", outcomes=(outcome(cost_usd=0.01), outcome(cost_usd=None)))
    assert arm.total_cost_usd is None
    assert ArmResult(arm="a", outcomes=(outcome(cost_usd=0.01),)).total_cost_usd == 0.01


def test_errored_cases_are_counted() -> None:
    assert ArmResult(arm="a", outcomes=(outcome(error="boom"),)).errored == 1


# ------------------------------------------------------------------ report ---


def test_the_report_names_its_false_positives() -> None:
    """A benchmark that hides its overclaims is a marketing document."""
    result = BenchmarkResult(
        arms=(
            ArmResult(
                arm="no-repair", outcomes=(outcome(claimed_verified=True, truly_correct=False),)
            ),
            ArmResult(arm="repair", outcomes=(outcome(claimed_verified=True, truly_correct=True),)),
        ),
        cases=1,
    )
    markdown = render_markdown(result)
    assert "Overclaimed" in markdown
    assert "0%" in markdown
    assert "100%" in markdown
    assert "Repair moved the proven success rate" in markdown


def test_the_report_names_ungraded_cases(tmp_path: Path) -> None:
    result = BenchmarkResult(arms=(ArmResult(arm="a"),), ungraded_cases=("07-case",))
    assert "07-case" in render_markdown(result)
    assert "ship no hidden test" in render_markdown(result)


def test_results_are_written_in_both_formats(tmp_path: Path) -> None:
    result = BenchmarkResult(arms=(ArmResult(arm="a", outcomes=(outcome(),)),), cases=1)
    json_path, markdown_path = write_results(result, tmp_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["arms"][0]["arm"] == "a"
    assert markdown_path.read_text(encoding="utf-8").startswith("# Migration benchmark")


def test_a_case_that_never_called_a_model_costs_zero_not_unknown(
    case: MigrationCase, settings: Settings, tmp_path: Path
) -> None:
    """One free case must not turn a whole arm's cost into "unknown"."""
    (case.directory / "new.yaml").write_text(
        SPEC.format(v="2", field="max_tokens"), encoding="utf-8"
    )
    outcome = evaluate_case(
        case,
        DEFAULT_ARMS[1],
        provider=provider(),
        settings=settings,
        verifier=verifier(migration=Verdict.VERIFIED, grading=Verdict.VERIFIED),
    )
    assert outcome.status is MigrationStatus.NO_BREAKING_CHANGES
    assert outcome.cost_usd == 0.0
    arm = ArmResult(
        arm="a",
        outcomes=(
            outcome,
            CaseOutcome(
                **{
                    "case_id": "c",
                    "arm": "a",
                    "expectation": Expectation.MIGRATE,
                    "status": MigrationStatus.VERIFIED,
                    "cost_usd": 0.5,
                }
            ),
        ),
    )
    assert arm.total_cost_usd == 0.5
