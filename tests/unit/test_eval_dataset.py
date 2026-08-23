"""Tests for loading labelled evaluation datasets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rewire.core.errors import EvaluationError
from rewire.evals.dataset import load_case, load_cases

DATASETS = Path("evals/datasets/impact")

LABELS = {
    "name": "example",
    "description": "d",
    "packages": ["openai"],
    "targets": [
        {
            "change_type": "request_field_removed",
            "field_path": "max_tokens",
            "expected": [{"file": "a.py", "line": 3, "reason": "why"}],
        }
    ],
}


def make_case(root: Path, labels: dict | None = None) -> Path:
    case = root / "case"
    (case / "repo").mkdir(parents=True)
    (case / "old.yaml").write_text("openapi: '3.0.3'\n", encoding="utf-8")
    (case / "new.yaml").write_text("openapi: '3.0.3'\n", encoding="utf-8")
    (case / "labels.json").write_text(json.dumps(labels or LABELS), encoding="utf-8")
    return case


def test_loads_a_case(tmp_path: Path) -> None:
    case = load_case(make_case(tmp_path))
    assert case.name == "example"
    assert case.packages == ("openai",)
    assert case.targets[0].expected[0].line == 3


def test_expected_keys_and_files(tmp_path: Path) -> None:
    target = load_case(make_case(tmp_path)).targets[0]
    assert target.expected_keys() == {("a.py", 3)}
    assert target.expected_files() == {"a.py"}
    assert target.label == "request_field_removed:max_tokens"


def test_missing_labels_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(EvaluationError, match="no labels file"):
        load_case(tmp_path)


def test_malformed_labels_are_an_error(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    (case / "labels.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(EvaluationError, match="could not read labels"):
        load_case(case)


def test_non_object_labels_are_an_error(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    (case / "labels.json").write_text("[]", encoding="utf-8")
    with pytest.raises(EvaluationError, match="must contain an object"):
        load_case(case)


def test_incomplete_case_is_an_error(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    (case / "new.yaml").unlink()
    with pytest.raises(EvaluationError, match="incomplete"):
        load_case(case)


def test_missing_dataset_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(EvaluationError, match="does not exist"):
        load_cases(tmp_path / "absent")


def test_empty_dataset_is_an_error(tmp_path: Path) -> None:
    """An empty benchmark reporting a perfect score is worse than an error."""
    with pytest.raises(EvaluationError, match="no cases"):
        load_cases(tmp_path)


def test_cases_load_sorted(tmp_path: Path) -> None:
    for name in ("zebra", "alpha"):
        case = tmp_path / name
        (case / "repo").mkdir(parents=True)
        (case / "old.yaml").write_text("x", encoding="utf-8")
        (case / "new.yaml").write_text("x", encoding="utf-8")
        (case / "labels.json").write_text(json.dumps({**LABELS, "name": name}), encoding="utf-8")
    assert [case.name for case in load_cases(tmp_path)] == ["alpha", "zebra"]


# ------------------------------------------------- the checked-in datasets ---


def test_shipped_datasets_load() -> None:
    cases = load_cases(DATASETS)
    assert {case.name for case in cases} >= {
        "decoys",
        "openai_max_tokens",
        "raw_http",
        "response_field",
        "unrelated",
    }


def test_every_expected_location_points_at_the_named_field() -> None:
    """Ground truth drifts when fixtures are edited; this catches it immediately."""
    for case in load_cases(DATASETS):
        for target in case.targets:
            leaf = (target.field_path or "").rsplit(".", maxsplit=1)[-1].removesuffix("[]")
            for expected in target.expected:
                source = (case.repository / expected.file).read_text(encoding="utf-8")
                line = source.splitlines()[expected.line - 1]
                assert leaf in line, f"{case.name} {expected.file}:{expected.line}"


def test_every_expected_location_carries_a_reason() -> None:
    """Ground truth is an opinion; an unexplained label cannot be reviewed."""
    for case in load_cases(DATASETS):
        for target in case.targets:
            assert all(expected.reason for expected in target.expected)


def test_at_least_one_case_expects_nothing() -> None:
    """Without a negative case a benchmark cannot distinguish care from eagerness."""
    cases = load_cases(DATASETS)
    assert any(all(not target.expected for target in case.targets) for case in cases)
