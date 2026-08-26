"""Tests for the cross-model comparison harness.

Every model and sandbox here is scripted; nothing calls a provider. What is
under test is whether the comparison stays honest — that a model with no
credential is reported rather than dropped, that a crashed model does not
discard the models that already ran, that overclaims are compared as carefully
as successes, and that the report is willing to say the difference is noise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from rewire.core.config import LLMSettings, Settings
from rewire.core.errors import ConfigurationError, EvaluationError
from rewire.evals.comparison import cell, render_money
from rewire.evals.migration_dataset import Expectation, MigrationCase
from rewire.evals.migration_runner import (
    DEFAULT_ARMS,
    ArmResult,
    BenchmarkResult,
    CaseOutcome,
)
from rewire.evals.model_matrix import (
    DEFAULT_COMPARISON_ARM,
    ComparisonConfig,
    ModelComparison,
    ModelRun,
    ModelSpec,
    _build_provider,
    compare_models,
    render_markdown,
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
        directory=directory,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings with an OpenAI key and no Anthropic one, as the project is configured."""
    return Settings(
        data_dir=tmp_path / ".rewire",
        llm=LLMSettings(provider="openai", openai_api_key=SecretStr("test-key")),
    )


def scripted_provider() -> ScriptedProvider:
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
    check = CheckResult(
        kind=CheckKind.TESTS,
        name="pytest",
        status=CheckStatus.PASSED if passed else CheckStatus.FAILED,
        outcome=CommandOutcome(command=("pytest",), exit_code=0 if passed else 1),
        reason="",
    )
    return VerificationReport(
        verdict=verdict, reason=f"scripted {verdict.value}", baseline=(check,), patched=(check,)
    )


def verifier(*, migration: Verdict, grading: Verdict):
    def verify(*_args: object, **kwargs: object) -> VerificationReport:
        return report_for(grading if kwargs.get("overlay") else migration)

    return verify


def factory(*_args: object) -> ScriptedProvider:
    return scripted_provider()


def outcome(case_id: str, *, correct: bool, claimed: bool = True) -> CaseOutcome:
    """A finished case, built directly so aggregation can be tested in isolation."""
    return CaseOutcome(
        case_id=case_id,
        arm="repair",
        expectation=Expectation.MIGRATE,
        status=MigrationStatus.VERIFIED if claimed else MigrationStatus.UNVERIFIED,
        claimed_verified=claimed,
        truly_correct=correct,
        tokens=100,
        cost_usd=0.01,
    )


def run_of(label: str, outcomes: tuple[CaseOutcome, ...], **extra: object) -> ModelRun:
    provider, _, model = label.partition(":")
    return ModelRun(
        label=label,
        provider=provider,
        model=model,
        result=BenchmarkResult(
            arms=(ArmResult(arm="repair", max_attempts=3, outcomes=outcomes),),
            provider=provider,
            model=model,
        ),
        **extra,
    )


# ---------------------------------------------------------- parsing specs ---


def test_a_spec_is_provider_and_model() -> None:
    spec = ModelSpec.parse("openai:gpt-4o")
    assert (spec.provider, spec.model, spec.label) == ("openai", "gpt-4o", "openai:gpt-4o")


def test_only_the_first_colon_separates() -> None:
    """Some hosted identifiers contain colons; splitting on all of them would mangle them."""
    assert ModelSpec.parse("openrouter:meta-llama/llama-3:free").model == "meta-llama/llama-3:free"


@pytest.mark.parametrize("text", ["gpt-4o", "openai:", ":gpt-4o", ""])
def test_a_malformed_spec_is_rejected(text: str) -> None:
    with pytest.raises(ConfigurationError, match="valid model specification"):
        ModelSpec.parse(text)


def test_a_provider_with_no_adapter_is_rejected_before_the_run() -> None:
    """Failing here costs nothing; failing after an hour of benchmarking costs money."""
    with pytest.raises(ConfigurationError, match="unknown provider"):
        ModelSpec.parse("acme:whisper-9")


# ------------------------------------------------------------- execution ---


def test_a_model_with_no_credential_is_reported_not_dropped(
    case: MigrationCase, settings: Settings, tmp_path: Path
) -> None:
    """A missing key must leave a visible hole in the report, not an invisible one."""
    comparison = compare_models(
        ComparisonConfig(
            dataset=tmp_path,
            models=(ModelSpec("anthropic", "claude-sonnet-5"),),
            results_dir=tmp_path / "results",
            incremental=False,
            provider_factory=factory,
        ),
        [case],
        settings=settings,
    )
    assert comparison.compared == ()
    (skipped,) = comparison.skipped
    assert "no API key for anthropic" in skipped.skipped
    assert "REWIRE_LLM__ANTHROPIC_API_KEY" in skipped.skipped
    assert "Not run" in render_markdown(comparison)


def test_a_model_that_crashes_does_not_discard_the_ones_that_ran(
    case: MigrationCase, settings: Settings, tmp_path: Path
) -> None:
    calls: list[str] = []

    def flaky(_settings: Settings, spec: ModelSpec) -> ScriptedProvider:
        calls.append(spec.model)
        if spec.model == "boom":
            raise RuntimeError("provider exploded")
        return scripted_provider()

    comparison = compare_models(
        ComparisonConfig(
            dataset=tmp_path,
            models=(ModelSpec("openai", "boom"), ModelSpec("openai", "gpt-4o")),
            results_dir=tmp_path / "results",
            incremental=False,
            provider_factory=flaky,
        ),
        [case],
        settings=settings,
        verifier=verifier(migration=Verdict.VERIFIED, grading=Verdict.VERIFIED),
    )
    assert calls == ["boom", "gpt-4o"]
    assert [run.label for run in comparison.compared] == ["openai:gpt-4o"]
    assert "provider exploded" in comparison.skipped[0].skipped


def test_every_model_runs_the_same_arm(
    case: MigrationCase, settings: Settings, tmp_path: Path
) -> None:
    comparison = compare_models(
        ComparisonConfig(
            dataset=tmp_path,
            models=(ModelSpec("openai", "gpt-4o"), ModelSpec("openai", "gpt-4o-mini")),
            results_dir=tmp_path / "results",
            incremental=False,
            provider_factory=factory,
        ),
        [case],
        settings=settings,
        verifier=verifier(migration=Verdict.VERIFIED, grading=Verdict.VERIFIED),
    )
    assert comparison.arm == DEFAULT_COMPARISON_ARM.name
    assert all(run.result is not None for run in comparison.compared)
    assert {run.total for run in comparison.compared} == {1}


def test_progress_names_the_model_being_run(
    case: MigrationCase, settings: Settings, tmp_path: Path
) -> None:
    seen: list[str] = []
    compare_models(
        ComparisonConfig(
            dataset=tmp_path,
            models=(ModelSpec("openai", "gpt-4o"),),
            results_dir=tmp_path / "results",
            incremental=False,
            provider_factory=factory,
            progress=seen.append,
        ),
        [case],
        settings=settings,
        verifier=verifier(migration=Verdict.VERIFIED, grading=Verdict.VERIFIED),
    )
    assert seen[0] == "openai:gpt-4o: starting"
    assert any("01-case" in message and message.startswith("openai:gpt-4o:") for message in seen)


def test_partial_results_survive_an_interruption(
    case: MigrationCase, settings: Settings, tmp_path: Path
) -> None:
    """A comparison costs real money; a kill must not throw away what it bought."""
    results = tmp_path / "results"
    compare_models(
        ComparisonConfig(
            dataset=tmp_path,
            models=(ModelSpec("openai", "gpt-4o"),),
            results_dir=results,
            provider_factory=factory,
        ),
        [case],
        settings=settings,
        verifier=verifier(migration=Verdict.VERIFIED, grading=Verdict.VERIFIED),
    )
    partial = json.loads((results / "models-partial.json").read_text(encoding="utf-8"))
    assert partial["runs"][0]["label"] == "openai:gpt-4o"


def test_comparing_nothing_is_an_error(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(EvaluationError, match="no models"):
        compare_models(ComparisonConfig(dataset=tmp_path, models=()), [], settings=settings)


def test_an_unpriced_model_reports_no_cost_rather_than_zero(
    case: MigrationCase, settings: Settings, tmp_path: Path
) -> None:
    """A model absent from the price table must not be reported as free."""
    comparison = compare_models(
        ComparisonConfig(
            dataset=tmp_path,
            models=(ModelSpec("openai", "gpt-9-imaginary"),),
            results_dir=tmp_path / "results",
            incremental=False,
            provider_factory=factory,
        ),
        [case],
        settings=settings,
        verifier=verifier(migration=Verdict.VERIFIED, grading=Verdict.VERIFIED),
    )
    assert comparison.compared[0].priced is False
    assert "absent from the pricing snapshot" in render_markdown(comparison)


# ----------------------------------------------------------- aggregation ---


def test_a_skipped_model_scores_nothing_and_pairs_with_nothing() -> None:
    """Scoring an unrun model zero would let a missing key look like a bad model."""
    run = ModelRun(label="anthropic:x", provider="anthropic", model="x", skipped="no key")
    assert run.correctness() == {}
    assert (run.succeeded, run.total, run.claimed, run.overclaimed, run.tokens) == (0, 0, 0, 0, 0)
    assert run.cost_usd is None
    assert run.overclaim_rate is None
    assert not run.ran


def test_overclaim_rate_is_a_share_of_what_was_vouched_for() -> None:
    """Two of three vouched-for patches wrong is 67%, not 40% of five cases."""
    run = run_of(
        "openai:gpt-4o",
        (
            outcome("a", correct=True),
            outcome("b", correct=False),
            outcome("c", correct=False),
            outcome("d", correct=False, claimed=False),
            outcome("e", correct=False, claimed=False),
        ),
    )
    assert run.claimed == 3
    assert run.overclaimed == 2
    assert run.overclaim_rate == pytest.approx(2 / 3)


def test_a_model_that_vouched_for_nothing_has_no_overclaim_rate() -> None:
    run = run_of("openai:gpt-4o", (outcome("a", correct=False, claimed=False),))
    assert run.overclaim_rate is None


def test_cases_no_model_solved_are_named_as_the_harness_ceiling() -> None:
    comparison = ModelComparison(
        runs=(
            run_of("openai:a", (outcome("solved", correct=True), outcome("hard", correct=False))),
            run_of("openai:b", (outcome("solved", correct=True), outcome("hard", correct=False))),
        )
    )
    assert comparison.unsolved() == ("hard",)
    assert comparison.solved_by_all() == ("solved",)


def test_agreement_needs_at_least_one_model() -> None:
    assert ModelComparison().unsolved() == ()
    assert ModelComparison().solved_by_all() == ()


def test_pairs_are_generated_for_every_combination() -> None:
    comparison = ModelComparison(
        runs=(
            run_of("openai:a", (outcome("x", correct=True),)),
            run_of("openai:b", (outcome("x", correct=False),)),
            run_of("openai:c", (outcome("x", correct=True),)),
        )
    )
    assert [(pair.a, pair.b) for pair in comparison.pairs()] == [
        ("openai:a", "openai:b"),
        ("openai:a", "openai:c"),
        ("openai:b", "openai:c"),
    ]


# --------------------------------------------------------------- report ---


def test_the_report_refuses_to_call_a_small_difference_a_result() -> None:
    """One case of difference is not a better model, and the report must say so."""
    comparison = ModelComparison(
        runs=(
            run_of("openai:a", (outcome("x", correct=True), outcome("y", correct=True))),
            run_of("openai:b", (outcome("x", correct=True), outcome("y", correct=False))),
        ),
        arm="repair",
    )
    markdown = render_markdown(comparison)
    assert "not distinguishable from chance" in markdown
    assert "Is the difference real?" in markdown


def test_the_report_shows_every_case_and_marks_overclaims() -> None:
    comparison = ModelComparison(
        runs=(
            run_of(
                "openai:a",
                (
                    outcome("x", correct=True),
                    outcome("y", correct=False),
                    outcome("z", correct=False, claimed=False),
                ),
            ),
        )
    )
    markdown = render_markdown(comparison)
    assert "## Case by case" in markdown
    assert "| `x` | ok |" in markdown
    assert "| `y` | **overclaim** |" in markdown
    assert "| `z` | miss |" in markdown


def test_the_report_marks_a_spurious_patch_on_a_no_op_case() -> None:
    """Patching code that needed no patch is its own failure, not a plain miss."""
    spurious = CaseOutcome(
        case_id="09-unrelated",
        arm="repair",
        expectation=Expectation.NO_OP,
        status=MigrationStatus.VERIFIED,
        claimed_verified=False,
    )
    markdown = render_markdown(ModelComparison(runs=(run_of("openai:a", (spurious,)),)))
    assert "**spurious**" in markdown


def test_the_report_marks_a_harness_error_distinctly() -> None:
    broken = CaseOutcome(
        case_id="x",
        arm="repair",
        expectation=Expectation.MIGRATE,
        status=MigrationStatus.NO_PATCH,
        error="Timeout",
    )
    markdown = render_markdown(ModelComparison(runs=(run_of("openai:a", (broken,)),)))
    assert "| `x` | error |" in markdown


def test_a_report_with_no_runs_says_it_measured_nothing() -> None:
    comparison = ModelComparison(
        runs=(ModelRun(label="anthropic:x", provider="anthropic", model="x", skipped="no key"),)
    )
    markdown = render_markdown(comparison)
    assert "**No model ran.** Nothing here is a measurement." in markdown
    assert "Case by case" not in markdown


def test_a_single_model_gets_no_agreement_section() -> None:
    """There is nothing to agree with, and an empty section would imply otherwise."""
    markdown = render_markdown(
        ModelComparison(runs=(run_of("openai:a", (outcome("x", correct=True),)),))
    )
    assert "What the models agree on" not in markdown


def test_the_agreement_section_reports_full_coverage_when_there_is_no_ceiling() -> None:
    comparison = ModelComparison(
        runs=(
            run_of("openai:a", (outcome("x", correct=True),)),
            run_of("openai:b", (outcome("x", correct=True),)),
        )
    )
    assert "Every case was solved by at least one model." in render_markdown(comparison)


def test_the_report_carries_the_pricing_snapshot_date() -> None:
    """Costs from a stale table must be visibly dated rather than assumed current."""
    comparison = ModelComparison(runs=(run_of("openai:a", (outcome("x", correct=True),)),))
    assert f"prices as of: {comparison.pricing_snapshot}" in render_markdown(comparison)


def test_results_are_written_as_json_and_markdown(tmp_path: Path) -> None:
    comparison = ModelComparison(
        runs=(run_of("openai:a", (outcome("x", correct=True),)),), arm="repair"
    )
    json_path, markdown_path = write_results(comparison, tmp_path / "out")
    assert json.loads(json_path.read_text(encoding="utf-8"))["arm"] == "repair"
    assert markdown_path.read_text(encoding="utf-8").startswith("# Model comparison")


def test_a_case_missing_from_one_model_renders_as_absent() -> None:
    """Columns must line up even when one model ran a shorter slice."""
    comparison = ModelComparison(
        runs=(
            run_of("openai:a", (outcome("x", correct=True), outcome("y", correct=True))),
            run_of("openai:b", (outcome("x", correct=True),)),
        )
    )
    assert "| `y` | ok | - |" in render_markdown(comparison)


def test_the_ceiling_and_the_freebies_are_both_named_in_the_report() -> None:
    """Both halves of the agreement structure have to reach the reader."""
    comparison = ModelComparison(
        runs=(
            run_of("openai:a", (outcome("solved", correct=True), outcome("hard", correct=False))),
            run_of("openai:b", (outcome("solved", correct=True), outcome("hard", correct=False))),
        )
    )
    markdown = render_markdown(comparison)
    assert "1 case(s) no model solved:** `hard`" in markdown
    assert "Rewire's ceiling rather than the model's" in markdown
    assert "1 case(s) every model solved:** `solved`" in markdown


def test_no_case_solved_by_everyone_omits_that_line() -> None:
    comparison = ModelComparison(
        runs=(
            run_of("openai:a", (outcome("x", correct=True),)),
            run_of("openai:b", (outcome("x", correct=False),)),
        )
    )
    assert "every model solved" not in render_markdown(comparison)


def test_a_cell_for_a_model_that_never_ran_is_blank() -> None:
    skipped = ModelRun(label="anthropic:x", provider="anthropic", model="x", skipped="no key")
    assert cell(skipped.contender, "anything") == "-"


def test_the_real_factory_builds_the_requested_model(settings: Settings) -> None:
    """The default path is exercised too, or the substitutable seam hides a broken default."""
    provider = _build_provider(settings, ModelSpec("openai", "gpt-4o-mini"))
    assert provider.name == "openai"
    assert provider.model == "gpt-4o-mini"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "unknown"),
        (0.0, "$0.00"),
        (0.0025, "$0.0025"),  # a third of a cent must not render as free
        (0.22, "$0.22"),
        (12.5, "$12.50"),
    ],
)
def test_costs_below_a_cent_keep_their_digits(value: float | None, expected: str) -> None:
    assert render_money(value) == expected


def test_models_are_compared_under_the_shipped_repair_arm() -> None:
    """A comparison run with a budget nobody ships describes a product nobody runs."""
    repair = next(arm for arm in DEFAULT_ARMS if arm.name == "repair")
    assert DEFAULT_COMPARISON_ARM is repair


def test_a_refused_patch_is_counted_separately_from_an_overclaim() -> None:
    """The distinction the whole weakening check exists to create.

    A patch Rewire refused to vouch for because it weakened the tests is still
    wrong, but it is not a *false claim*. Folding the two together would make
    the check look like it had done nothing.
    """
    refused = CaseOutcome(
        case_id="08-wrapper-and-tests",
        arm="repair",
        expectation=Expectation.MIGRATE,
        status=MigrationStatus.UNVERIFIED,
        claimed_verified=False,
        truly_correct=False,
        verdicts=("weakened", "weakened"),
    )
    comparison = ModelComparison(
        runs=(run_of("openai:a", (refused, outcome("01", correct=False))),)
    )
    column = comparison.contenders[0]
    assert column.refused_as_weakened == 1
    assert column.overclaimed == 1

    markdown = render_markdown(comparison)
    assert "Overclaim rate | Weakened |" in markdown
