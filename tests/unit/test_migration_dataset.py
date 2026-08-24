"""Tests for the migration benchmark dataset and its loader.

The dataset validation here is not decoration. Three properties silently
destroy the measurement if they stop holding, and none of them is visible by
reading a case:

* the visible tests must pass on the unmodified repository, or the baseline is
  red and nothing can ever be verified;
* the hidden tests must **fail** on the unmodified repository, or the case is
  passed without migrating anything and grades nothing at all;
* a no-op case is the mirror image: its hidden tests must pass untouched.

A hidden test that already passes is the benchmark equivalent of a test with no
assertion, and it would inflate every number in the report.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from rewire.analyzers import build_index
from rewire.changes import diff_specs, load_spec
from rewire.core.errors import EvaluationError
from rewire.evals.migration_dataset import (
    Expectation,
    load_migration_case,
    load_migration_cases,
)
from rewire.impact import analyse_impact

DATASET = Path("evals/datasets/migration")

CASES = load_migration_cases(DATASET)


def run_pytest(root: Path) -> tuple[int, str]:
    """Run a case repository's tests in isolation, without installing it."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"},
    )
    return done.returncode, (done.stdout + done.stderr)[-600:]


# ------------------------------------------------------------------ loading ---


def test_the_dataset_loads() -> None:
    assert len(CASES) >= 10
    assert len({case.case_id for case in CASES}) == len(CASES)


def test_every_case_explains_its_expectation() -> None:
    """Ground truth nobody can argue with is ground truth nobody checked."""
    for case in CASES:
        assert case.rationale, case.case_id
        assert case.description, case.case_id
        assert case.tags, case.case_id


def test_a_missing_manifest_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(EvaluationError, match="manifest is missing"):
        load_migration_case(tmp_path)


def test_a_malformed_manifest_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "case.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(EvaluationError, match="could not read"):
        load_migration_case(tmp_path)


def test_an_incomplete_case_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "case.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EvaluationError, match="incomplete"):
        load_migration_case(tmp_path)


def test_a_case_missing_a_spec_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "case.json").write_text("{}", encoding="utf-8")
    (tmp_path / "repo").mkdir()
    with pytest.raises(EvaluationError, match="incomplete"):
        load_migration_case(tmp_path)


def test_an_invalid_manifest_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "case.json").write_text(json.dumps({"case_id": "x"}), encoding="utf-8")
    (tmp_path / "repo").mkdir()
    (tmp_path / "old.yaml").write_text("openapi: '3.0.3'\n", encoding="utf-8")
    (tmp_path / "new.yaml").write_text("openapi: '3.0.3'\n", encoding="utf-8")
    with pytest.raises(EvaluationError, match="invalid"):
        load_migration_case(tmp_path)


def test_a_missing_dataset_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(EvaluationError, match="does not exist"):
        load_migration_cases(tmp_path / "absent")


def test_an_empty_dataset_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(EvaluationError, match="holds no cases"):
        load_migration_cases(tmp_path)


def test_a_case_without_hidden_tests_reports_nothing(tmp_path: Path) -> None:
    """Reported as empty rather than as a pass, so it cannot inflate a score."""
    case = CASES[0]
    stripped = case.model_copy(update={"directory": tmp_path})
    assert stripped.hidden_tests() == {}


# --------------------------------------------------------------- validation ---


@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_each_case_has_a_breaking_change_to_detect(case) -> None:
    changes = diff_specs(load_spec(case.old_spec), load_spec(case.new_spec))
    assert changes.summary.breaking + changes.summary.potentially_breaking > 0


@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_impact_matches_the_expectation(case) -> None:
    """A migrate case must have affected code; a no-op case must have none.

    One case is tagged ``limitation:nothing-to-match`` and is exempt. Impact
    analysis finds affected code by matching names that appear in it, so a
    change requiring the repository to start sending a field it has never sent
    has no anchor to find. That case is in the dataset precisely because Rewire
    fails it, and it is expected to score zero until that changes.
    """
    changes = diff_specs(load_spec(case.old_spec), load_spec(case.new_spec))
    impact = analyse_impact(changes, build_index(case.repository), packages=case.packages)
    if "limitation:nothing-to-match" in case.tags:
        assert impact.summary.locations == 0
    elif case.expectation is Expectation.MIGRATE:
        assert impact.summary.locations > 0
    else:
        assert impact.summary.locations == 0


@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_the_visible_tests_pass_before_migration(case) -> None:
    """A red baseline can never be verified, so the case would grade nothing."""
    code, output = run_pytest(case.repository)
    assert code == 0, output


@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_the_hidden_tests_reject_the_unmigrated_repository(case) -> None:
    """A hidden test that already passes grades nothing and inflates the score."""
    hidden = case.hidden_tests()
    assert hidden, f"{case.case_id} ships no hidden test"

    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "repo"
        shutil.copytree(case.repository, copy)
        for relative, content in hidden.items():
            target = copy / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        code, output = run_pytest(copy)

    if case.expectation is Expectation.MIGRATE:
        assert code != 0, f"{case.case_id} passes its hidden test without migrating:\n{output}"
    else:
        assert code == 0, f"{case.case_id} is a no-op case but its hidden test fails:\n{output}"
