"""Tests for the ablation arms, their reporting, and what they must not share.

The arms themselves are configuration, and configuration is exactly where an
experiment goes wrong quietly: an arm that varies two things measures neither,
and an arm that varies nothing reports the control's number under another name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rewire.agents.config import ALL_TOOLS, SEARCH_TOOLS
from rewire.evals.ablation import (
    ABLATION_ATTEMPTS,
    DEFAULT_ABLATIONS,
    contenders,
    render_markdown,
    write_results,
)
from rewire.evals.migration_dataset import Expectation
from rewire.evals.migration_runner import ArmConfig, ArmResult, BenchmarkResult, CaseOutcome
from rewire.services.migrate import MigrationStatus


def outcome(case_id: str, *, correct: bool | None, claimed: bool = True) -> CaseOutcome:
    return CaseOutcome(
        case_id=case_id,
        arm="arm",
        expectation=Expectation.MIGRATE,
        status=MigrationStatus.VERIFIED if claimed else MigrationStatus.UNVERIFIED,
        claimed_verified=claimed,
        truly_correct=correct,
        tokens=1000,
        cost_usd=0.01,
    )


def arm(name: str, correct: tuple[bool, ...], *, harness: str = "full configuration") -> ArmResult:
    return ArmResult(
        arm=name,
        description=f"{name} description",
        max_attempts=ABLATION_ATTEMPTS,
        harness=harness,
        outcomes=tuple(outcome(f"0{i + 1}", correct=value) for i, value in enumerate(correct)),
    )


def benchmark(*arms: ArmResult) -> BenchmarkResult:
    return BenchmarkResult(
        arms=arms,
        provider="openai",
        model="gpt-4o",
        dataset="evals/datasets/migration",
        cases=max((a.total for a in arms), default=0),
    )


# ---------------------------------------------------------------- the arms ---


def test_the_first_arm_is_the_shipped_configuration() -> None:
    """A comparison without a control measures nothing."""
    control = DEFAULT_ABLATIONS[0]
    assert control.name == "full"
    assert control.is_control
    assert control.agent.is_default


def test_every_arm_gets_the_same_repair_budget() -> None:
    """Repair was measured in Phase 8; varying it here would confound this one."""
    assert {a.max_attempts for a in DEFAULT_ABLATIONS} == {ABLATION_ATTEMPTS}


def test_every_arm_has_a_distinct_name_and_a_distinct_harness() -> None:
    names = [a.name for a in DEFAULT_ABLATIONS]
    harnesses = [a.describe_harness() for a in DEFAULT_ABLATIONS]
    assert len(set(names)) == len(names)
    assert len(set(harnesses)) == len(harnesses)


def test_only_the_control_is_the_shipped_configuration() -> None:
    """An arm that changes nothing would report the control's score under its own name."""
    assert [a.name for a in DEFAULT_ABLATIONS if a.is_control] == ["full"]


def test_the_withheld_arms_still_receive_the_api_changes() -> None:
    """These arms remove the answer, not the question."""
    for config in DEFAULT_ABLATIONS:
        if not config.include_impact_locations:
            assert config.agent.tools == ALL_TOOLS, config.name


def test_the_search_arm_keeps_the_locations_and_loses_the_tools() -> None:
    """It is the mirror image of the withheld arms, not another copy of them."""
    config = next(a for a in DEFAULT_ABLATIONS if a.name == "no-search")
    assert config.include_impact_locations
    assert config.agent.tools == ALL_TOOLS - SEARCH_TOOLS


def test_only_one_arm_bypasses_the_no_affected_code_gate() -> None:
    bypassing = [a.name for a in DEFAULT_ABLATIONS if not a.require_affected_code]
    assert bypassing == ["no-impact"]


def test_an_arm_naming_a_tool_that_does_not_exist_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        _ = ArmConfig(name="typo", max_attempts=1, withheld_tools=("serch_code",)).agent


# ------------------------------------------------------------- the report ---


def test_the_report_names_what_each_arm_lost() -> None:
    result = benchmark(arm("full", (True, True)), arm("no-search", (True, False)))
    markdown = render_markdown(result)
    assert "| Arm | What it lost |" in markdown
    assert "| `no-search` | no-search description |" in markdown


def test_the_report_refuses_to_call_a_one_case_gap_a_result() -> None:
    result = benchmark(arm("full", (True, True)), arm("no-impact", (True, False)))
    assert "not distinguishable from chance" in render_markdown(result)


def test_the_report_names_cases_no_arm_reached() -> None:
    """A case no configuration solves is not a question of what the agent was given."""
    result = benchmark(arm("full", (True, False)), arm("no-impact", (True, False)))
    markdown = render_markdown(result)
    assert "1 case(s) no arm solved:** `02`" in markdown
    assert "No configuration of the harness reached them" in markdown


def test_the_report_carries_the_ungraded_warning() -> None:
    result = benchmark(arm("full", (True,))).model_copy(update={"ungraded_cases": ("09",)})
    assert "ship no hidden test" in render_markdown(result)


def test_the_report_states_what_was_held_constant() -> None:
    """A reader has to be able to see that only one thing varied."""
    markdown = render_markdown(benchmark(arm("full", (True,))))
    assert "identical for every arm" in markdown
    assert f"repair budget: {ABLATION_ATTEMPTS} attempts" in markdown


def test_contenders_carry_the_harness_description() -> None:
    result = benchmark(arm("no-search", (True,), harness="tools withheld: search_code"))
    assert contenders(result)[0].note == "tools withheld: search_code"


def test_results_are_written_as_json_and_markdown(tmp_path: Path) -> None:
    result = benchmark(arm("full", (True,)))
    json_path, markdown_path = write_results(result, tmp_path / "out")
    assert json.loads(json_path.read_text(encoding="utf-8"))["arms"][0]["arm"] == "full"
    assert markdown_path.read_text(encoding="utf-8").startswith("# Agent ablations")
