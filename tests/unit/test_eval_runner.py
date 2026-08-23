"""Tests for the impact evaluation runner, including its published results."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rewire.core.errors import EvaluationError
from rewire.evals import render_markdown, run_evaluation, write_results
from rewire.evals.impact_runner import EvaluationResult

DATASETS = Path("evals/datasets/impact")


@pytest.fixture(scope="module")
def result() -> EvaluationResult:
    """One run shared across assertions; evaluation is deterministic."""
    return run_evaluation(DATASETS, version="test")


def test_every_case_is_evaluated(result: EvaluationResult) -> None:
    assert len(result.cases) >= 5
    assert all(case.targets for case in result.cases)


def test_the_benchmark_is_currently_perfect(result: EvaluationResult) -> None:
    """A regression in scoring should fail here before it reaches the README."""
    assert result.location_metrics.f1 == 1.0
    assert result.file_metrics.f1 == 1.0


def test_the_benchmark_is_not_trivially_small(result: EvaluationResult) -> None:
    """A perfect score over two locations would mean nothing."""
    assert result.location_metrics.counts.expected >= 9


def test_every_labelled_change_was_detected(result: EvaluationResult) -> None:
    """A change the differ never produced cannot be scored as an impact miss."""
    assert result.undetected_changes == []


def test_results_are_deterministic() -> None:
    first = run_evaluation(DATASETS, version="test")
    second = run_evaluation(DATASETS, version="test")
    assert first.model_dump_json(exclude={"duration_seconds", "cases"}) == (
        second.model_dump_json(exclude={"duration_seconds", "cases"})
    )


def test_configuration_is_recorded(result: EvaluationResult) -> None:
    """A number without the settings that produced it cannot be compared."""
    assert result.config.rewire_version == "test"
    assert result.config.min_confidence > 0
    assert result.config.dataset_dir


def test_a_higher_threshold_trades_recall_for_precision() -> None:
    strict = run_evaluation(DATASETS, min_confidence=0.999)
    assert strict.location_metrics.recall < 1.0


def test_missing_dataset_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(EvaluationError):
        run_evaluation(tmp_path / "absent")


# ------------------------------------------------------------------ output ---


def test_markdown_reports_both_granularities(result: EvaluationResult) -> None:
    rendered = render_markdown(result)
    assert "| location |" in rendered
    assert "| file |" in rendered


def test_markdown_records_the_configuration(result: EvaluationResult) -> None:
    rendered = render_markdown(result)
    assert "Minimum confidence" in rendered
    assert "test" in rendered


def test_markdown_describes_every_case(result: EvaluationResult) -> None:
    rendered = render_markdown(result)
    for case in result.cases:
        assert f"`{case.name}`" in rendered
        assert case.description in rendered


def test_write_results_emits_json_and_markdown(result: EvaluationResult, tmp_path: Path) -> None:
    json_path, markdown_path = write_results(result, tmp_path)
    assert json_path.name == "latest.json"
    assert markdown_path.name == "latest.md"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["location_metrics"]["f1"] == 1.0
    assert markdown_path.read_text(encoding="utf-8").startswith("# Impact analysis")


def test_write_results_reports_an_unwritable_directory(
    result: EvaluationResult, tmp_path: Path
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a directory", encoding="utf-8")
    with pytest.raises(EvaluationError, match="could not write results"):
        write_results(result, blocked)


def test_failures_are_itemised_for_diagnosis() -> None:
    """A false positive must be diagnosable from the report alone."""
    strict = run_evaluation(DATASETS, min_confidence=0.999)
    missed = [item for case in strict.cases for target in case.targets for item in target.missed]
    assert missed
    assert all(item.reason for item in missed)


def test_a_change_the_differ_never_reported_is_distinguished(tmp_path: Path) -> None:
    """Scoring it as an impact miss would blame Phase 3 for a Phase 1 gap."""
    case = tmp_path / "case"
    (case / "repo").mkdir(parents=True)
    spec = 'openapi: "3.0.3"\ninfo: {title: T, version: "1"}\npaths: {}\n'
    (case / "old.yaml").write_text(spec, encoding="utf-8")
    (case / "new.yaml").write_text(spec, encoding="utf-8")
    (case / "labels.json").write_text(
        json.dumps(
            {
                "name": "phantom",
                "targets": [
                    {
                        "change_type": "request_field_removed",
                        "field_path": "never_detected",
                        "expected": [{"file": "a.py", "line": 1, "reason": "r"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = run_evaluation(tmp_path)
    assert result.undetected_changes == ["phantom/request_field_removed:never_detected"]
    assert "never reported" in render_markdown(result)
    # It still counts as a miss, so the headline number is not flattered.
    assert result.location_metrics.recall == 0.0


def test_case_lookup_finds_a_labelled_target() -> None:
    from rewire.evals import load_case

    case = load_case(DATASETS / "openai_max_tokens")
    assert case.target_for("request_field_removed", "max_tokens") is not None
    assert case.target_for("request_field_removed", "absent") is None


def test_spurious_locations_are_itemised_in_the_report() -> None:
    """A false positive must be diagnosable without re-running the analysis."""
    permissive = run_evaluation(DATASETS, min_confidence=0.0)
    spurious = [
        item for case in permissive.cases for target in case.targets for item in target.spurious
    ]
    assert spurious
    assert all(item.top_signal for item in spurious)
    assert "spurious" in render_markdown(permissive)
