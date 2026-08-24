"""Tests for the benchmark CLI commands.

The runners themselves are tested in ``test_migration_runner`` and
``test_model_matrix``; what is covered here is the layer between a user's
arguments and those runners — argument validation, what reaches the terminal,
and whether ``--no-write`` genuinely writes nothing.

Both commands spend real money when they run for real, so every test here
substitutes the runner. That substitution is also the point of the test: a
command that could not be driven without a provider could not be tested at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rewire import cli
from rewire.cli import app
from rewire.core.errors import ConfigurationError
from rewire.evals.migration_dataset import Expectation
from rewire.evals.migration_runner import ArmResult, BenchmarkResult, CaseOutcome
from rewire.evals.model_matrix import ModelComparison, ModelRun
from rewire.services.migrate import MigrationStatus

runner = CliRunner()

DATASET = Path("evals/datasets/migration")


def flat(text: str) -> str:
    """Collapse Rich's line wrapping, so a prose assertion is not a layout assertion."""
    return " ".join(text.split())


def failure(result: object) -> ConfigurationError:
    """The domain error a command exited on.

    The CLI's own entry point renders these; the test runner captures them on
    the result instead, so the assertion has to look there.
    """
    exception = result.exception  # type: ignore[attr-defined]
    assert isinstance(exception, ConfigurationError), exception
    return exception


@pytest.fixture(autouse=True)
def _quiet_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWIRE_LOG_LEVEL", "WARNING")


def outcome(case_id: str, *, correct: bool | None, claimed: bool = True) -> CaseOutcome:
    return CaseOutcome(
        case_id=case_id,
        arm="repair",
        expectation=Expectation.MIGRATE,
        status=MigrationStatus.VERIFIED if claimed else MigrationStatus.UNVERIFIED,
        claimed_verified=claimed,
        truly_correct=correct,
        tokens=1000,
        cost_usd=0.02,
    )


def benchmark() -> BenchmarkResult:
    return BenchmarkResult(
        arms=(
            ArmResult(
                arm="no-repair",
                description="one attempt",
                max_attempts=1,
                outcomes=(outcome("01", correct=True), outcome("02", correct=False)),
            ),
            ArmResult(
                arm="repair",
                description="three attempts",
                max_attempts=3,
                outcomes=(outcome("01", correct=True), outcome("02", correct=True)),
            ),
        ),
        provider="openai",
        model="gpt-4o",
        dataset=str(DATASET),
        cases=2,
        ungraded_cases=("02",),
    )


def comparison() -> ModelComparison:
    def run(label: str, correct: tuple[bool, ...]) -> ModelRun:
        provider, _, model = label.partition(":")
        return ModelRun(
            label=label,
            provider=provider,
            model=model,
            result=BenchmarkResult(
                arms=(
                    ArmResult(
                        arm="repair",
                        max_attempts=3,
                        outcomes=tuple(
                            outcome(f"0{i + 1}", correct=value) for i, value in enumerate(correct)
                        ),
                    ),
                ),
                provider=provider,
                model=model,
            ),
        )

    return ModelComparison(
        runs=(
            run("openai:gpt-4o", (True, False)),
            run("openai:gpt-4o-mini", (False, False)),
            ModelRun(
                label="anthropic:claude-sonnet-5",
                provider="anthropic",
                model="claude-sonnet-5",
                skipped="no API key for anthropic",
            ),
        ),
        arm="repair",
        dataset=str(DATASET),
        cases=2,
    )


@pytest.fixture
def stub_migrate(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace the benchmark runner and the provider construction it needs."""
    seen: list[object] = []

    def fake_run_benchmark(config: object, cases: object, **_kwargs: object) -> BenchmarkResult:
        seen.append(config)
        return benchmark()

    monkeypatch.setattr(cli, "run_benchmark", fake_run_benchmark)
    monkeypatch.setattr(cli, "build_provider", lambda _settings: object())
    return seen


@pytest.fixture
def stub_models(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    seen: list[object] = []

    def fake_compare(config: object, cases: object, **_kwargs: object) -> ModelComparison:
        seen.append(config)
        return comparison()

    monkeypatch.setattr(cli, "compare_models", fake_compare)
    return seen


# ------------------------------------------------------------ eval migrate ---


def test_eval_migrate_reports_claimed_beside_correct(
    stub_migrate: list[object], tmp_path: Path
) -> None:
    """The headline table is worthless without the overclaim column next to it."""
    result = runner.invoke(app, ["eval", "migrate", "--no-write", "--results-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "Overclaimed" in result.stdout
    assert "no-repair" in result.stdout and "repair" in result.stdout
    assert "never visible to the agent" in flat(result.stdout)


def test_eval_migrate_warns_when_cases_grade_themselves(
    stub_migrate: list[object], tmp_path: Path
) -> None:
    result = runner.invoke(app, ["eval", "migrate", "--no-write", "--results-dir", str(tmp_path)])
    assert "graded on Rewire's own word" in flat(result.stdout)


def test_eval_migrate_writes_both_report_formats(
    stub_migrate: list[object], tmp_path: Path
) -> None:
    result = runner.invoke(app, ["eval", "migrate", "--results-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "migration.json").is_file()
    assert (tmp_path / "migration.md").is_file()


def test_no_write_leaves_the_results_directory_alone(
    stub_migrate: list[object], tmp_path: Path
) -> None:
    """Including the crash-recovery partial, or the flag quietly lies."""
    result = runner.invoke(app, ["eval", "migrate", "--no-write", "--results-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert list(tmp_path.iterdir()) == []
    assert stub_migrate[0].incremental is False  # type: ignore[attr-defined]


def test_eval_migrate_selects_arms(stub_migrate: list[object], tmp_path: Path) -> None:
    runner.invoke(
        app,
        ["eval", "migrate", "--no-write", "--results-dir", str(tmp_path), "--arm", "repair"],
    )
    assert [arm.name for arm in stub_migrate[0].arms] == ["repair"]  # type: ignore[attr-defined]


def test_an_unknown_arm_is_rejected(stub_migrate: list[object], tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["eval", "migrate", "--no-write", "--results-dir", str(tmp_path), "--arm", "nope"],
    )
    assert result.exit_code != 0
    assert "no matching arm" in str(failure(result))


def test_eval_migrate_passes_the_case_filter_through(
    stub_migrate: list[object], tmp_path: Path
) -> None:
    runner.invoke(
        app,
        [
            "eval",
            "migrate",
            "--no-write",
            "--results-dir",
            str(tmp_path),
            "--case",
            "01-request-field-renamed",
            "--limit",
            "1",
        ],
    )
    assert stub_migrate[0].only == ("01-request-field-renamed",)  # type: ignore[attr-defined]
    assert stub_migrate[0].limit == 1  # type: ignore[attr-defined]


# ------------------------------------------------------------- eval models ---


def test_eval_models_needs_at_least_one_model() -> None:
    """Failing here costs nothing; failing after an hour of benchmarking costs money."""
    result = runner.invoke(app, ["eval", "models", "--no-write"])
    assert result.exit_code != 0
    assert "no models to compare" in str(failure(result))


def test_a_malformed_model_is_rejected_before_anything_runs(
    stub_models: list[object], tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        ["eval", "models", "--no-write", "--results-dir", str(tmp_path), "--model", "gpt-4o"],
    )
    assert "valid model specification" in str(failure(result))
    assert stub_models == []


def test_eval_models_reports_intervals_and_significance(
    stub_models: list[object], tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "eval",
            "models",
            "--no-write",
            "--results-dir",
            str(tmp_path),
            "--model",
            "openai:gpt-4o",
            "--model",
            "openai:gpt-4o-mini",
        ],
    )
    assert result.exit_code == 0
    assert "95% CI" in flat(result.stdout)
    assert "not distinguishable from chance" in flat(result.stdout)


def test_eval_models_names_the_ceiling_and_the_skipped(
    stub_models: list[object], tmp_path: Path
) -> None:
    """A missing key and an unsolvable case both have to reach the terminal."""
    result = runner.invoke(
        app,
        [
            "eval",
            "models",
            "--no-write",
            "--results-dir",
            str(tmp_path),
            "--model",
            "openai:gpt-4o",
        ],
    )
    assert "no model solved" in flat(result.stdout)
    assert "skipped anthropic:claude-sonnet-5" in flat(result.stdout)


def test_eval_models_writes_both_report_formats(stub_models: list[object], tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "eval",
            "models",
            "--results-dir",
            str(tmp_path),
            "--model",
            "openai:gpt-4o",
        ],
    )
    assert result.exit_code == 0
    assert (tmp_path / "models.json").is_file()
    assert (tmp_path / "models.md").is_file()


def test_eval_models_applies_one_repair_budget_to_every_model(
    stub_models: list[object], tmp_path: Path
) -> None:
    """A comparison where the arms differ is not a comparison."""
    runner.invoke(
        app,
        [
            "eval",
            "models",
            "--no-write",
            "--results-dir",
            str(tmp_path),
            "--model",
            "openai:gpt-4o",
            "--max-attempts",
            "5",
        ],
    )
    assert stub_models[0].arm.max_attempts == 5  # type: ignore[attr-defined]
    assert stub_models[0].incremental is False  # type: ignore[attr-defined]


def test_eval_help_lists_both_benchmarks() -> None:
    result = runner.invoke(app, ["eval", "--help"])
    assert result.exit_code == 0
    assert "migrate" in result.stdout
    assert "models" in result.stdout
