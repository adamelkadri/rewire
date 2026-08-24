"""Tests for the ablation knobs, and for the ways an ablation can silently fail.

An ablation that does not actually withhold anything is the worst kind of bug in
an experiment: it produces the control's number under the ablation's label, and
nothing crashes. Most of these tests exist for that failure mode — the misspelt
tool name, the second channel left open, the tool the model guesses the name of.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rewire.agents.config import (
    ALL_TOOLS,
    DEFAULT_AGENT_CONFIG,
    SEARCH_TOOLS,
    AgentConfig,
)
from rewire.agents.patch import PatchBuilder
from rewire.agents.prompts import build_task_prompt
from rewire.agents.tools import TOOLS_BY_NAME, ToolContext, invoke, tool_specs
from rewire.agents.workspace import Workspace
from rewire.analyzers.index import build_index
from rewire.changes.differ import diff_specs
from rewire.changes.spec import load_spec
from rewire.impact.analyzer import analyse_impact

CASE = Path("evals/datasets/migration/01-request-field-renamed")


@pytest.fixture(scope="module")
def pieces() -> tuple[object, object, object]:
    """A real change report, index and impact report from a benchmark case."""
    changes = diff_specs(load_spec(CASE / "old.yaml"), load_spec(CASE / "new.yaml"))
    index = build_index(CASE / "repo")
    impact = analyse_impact(changes, index)
    return changes, index, impact


def context(pieces: tuple[object, object, object], *, locations: bool) -> ToolContext:
    changes, index, impact = pieces
    workspace = Workspace.open(CASE / "repo")
    return ToolContext(
        workspace=workspace,
        index=index,  # type: ignore[arg-type]
        changes=changes,  # type: ignore[arg-type]
        impact=impact,  # type: ignore[arg-type]
        patch=PatchBuilder(read_file=workspace.read_full),
        include_impact_locations=locations,
    )


# ------------------------------------------------------------- the config ---


def test_the_default_is_the_shipped_configuration() -> None:
    assert DEFAULT_AGENT_CONFIG.is_default
    assert DEFAULT_AGENT_CONFIG.tools == ALL_TOOLS
    assert DEFAULT_AGENT_CONFIG.describe() == "full configuration"


def test_withholding_is_named_in_the_description() -> None:
    """The trace records this string, so a result cannot be filed under the wrong arm."""
    config = AgentConfig.without(impact_locations=False, tools=SEARCH_TOOLS)
    described = config.describe()
    assert "impact locations withheld" in described
    assert "search_code" in described
    assert not config.is_default


def test_a_misspelt_tool_name_is_rejected_rather_than_ignored() -> None:
    """Subtracting a name that does not exist would withhold nothing at all."""
    with pytest.raises(ValueError, match="unknown tool"):
        AgentConfig.without(tools=["serch_code"])
    with pytest.raises(ValueError, match="unknown tool"):
        AgentConfig(tools=frozenset({"not_a_tool"}))


def test_search_tools_are_a_real_subset_of_the_tools() -> None:
    """A constant that drifted out of the registry would silently withhold nothing."""
    assert SEARCH_TOOLS < ALL_TOOLS
    assert frozenset(TOOLS_BY_NAME) == ALL_TOOLS


# -------------------------------------------------------------- the tools ---


def test_offered_tools_are_filtered_and_keep_their_order() -> None:
    """Order forms part of a cached prompt prefix; withholding must not reorder."""
    full = [spec.name for spec in tool_specs()]
    reduced = [spec.name for spec in tool_specs(ALL_TOOLS - SEARCH_TOOLS)]
    assert set(reduced) == ALL_TOOLS - SEARCH_TOOLS
    assert reduced == [name for name in full if name in set(reduced)]


def test_a_withheld_tool_cannot_be_invoked_by_guessing_its_name(
    pieces: tuple[object, object, object],
) -> None:
    """Omitting a tool from the specifications is not enough on its own.

    A model that produces the name anyway would otherwise get a working tool,
    and the ablation would leak through a lucky guess.
    """
    allowed = ALL_TOOLS - SEARCH_TOOLS
    result = invoke(
        "search_code", context(pieces, locations=True), {"name": "max_tokens"}, allowed=allowed
    )
    assert result.is_error
    assert "Unknown tool" in result.content
    # The list of what it *may* call must not advertise what was taken away.
    assert "Available: inspect_api_change, list_files" in result.content
    assert "find_calls" not in result.content


def test_an_allowed_tool_still_runs(pieces: tuple[object, object, object]) -> None:
    result = invoke(
        "search_code",
        context(pieces, locations=True),
        {"name": "max_tokens"},
        allowed=ALL_TOOLS,
    )
    assert not result.is_error


def test_inspect_api_change_withholds_locations_too(
    pieces: tuple[object, object, object],
) -> None:
    """The task prompt is not the only channel the locations travel down."""
    shown = invoke("inspect_api_change", context(pieces, locations=True), {})
    hidden = invoke("inspect_api_change", context(pieces, locations=False), {})
    assert "confidence" in shown.content
    assert "confidence" not in hidden.content
    assert "find them yourself" in hidden.content
    # What changed is still disclosed; only where it is used is withheld.
    assert "max_tokens" in hidden.content


# ------------------------------------------------------------- the prompt ---


def test_the_task_prompt_names_the_locations_by_default(
    pieces: tuple[object, object, object],
) -> None:
    changes, _, impact = pieces
    prompt = build_task_prompt(changes, impact)  # type: ignore[arg-type]
    assert "affected locations:" in prompt
    assert "Read each location before editing it." in prompt


def test_the_task_prompt_withholds_locations_and_says_so(
    pieces: tuple[object, object, object],
) -> None:
    changes, _, impact = pieces
    prompt = build_task_prompt(changes, impact, include_locations=False)  # type: ignore[arg-type]
    assert "affected locations:" not in prompt
    assert "confidence" not in prompt
    assert "find every use yourself" in prompt
    # The API change itself is still given: this ablation removes the answer,
    # not the question.
    assert "max_tokens" in prompt


def test_the_withheld_prompt_lists_changes_impact_found_no_code_for(
    pieces: tuple[object, object, object],
) -> None:
    """Filtering by impact would leak its findings through what is mentioned.

    An arm that only hears about the changes impact analysis located code for is
    still being helped by impact analysis, which is exactly what it is supposed
    to be doing without.
    """
    changes, _, impact = pieces
    withheld = build_task_prompt(changes, impact, include_locations=False)  # type: ignore[arg-type]
    shown = build_task_prompt(changes, impact)  # type: ignore[arg-type]
    assert withheld.count("###") >= shown.count("###")


def test_a_prompt_with_no_changes_at_all_says_so() -> None:
    """The withheld arm can run on a repository impact analysis found nothing in."""
    from rewire.impact.models import ImpactReport

    changes = diff_specs(load_spec(CASE / "old.yaml"), load_spec(CASE / "new.yaml"))
    empty = ImpactReport(repository=str(CASE / "repo"))
    prompt = build_task_prompt(changes, empty, include_locations=False)
    assert "No API changes were detected." in prompt


def test_an_agent_reports_the_configuration_it_was_built_with() -> None:
    """The trace records this, and a run filed under the wrong arm is worthless."""
    from rewire.agents.migration_agent import MigrationAgent
    from rewire.llm import ScriptBuilder

    provider = ScriptBuilder().says("done").build()
    assert MigrationAgent(provider).config.is_default

    withheld = AgentConfig.without(impact_locations=False)
    assert MigrationAgent(provider, config=withheld).config is withheld
