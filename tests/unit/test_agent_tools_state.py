"""Tests for the agent tool surface, state machine, tracing and registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from rewire.agents.patch import PatchBuilder
from rewire.agents.state import (
    ALLOWED_TRANSITIONS,
    AgentState,
    can_transition,
    is_terminal,
)
from rewire.agents.tools import TOOLS, TOOLS_BY_NAME, ToolContext, invoke, tool_specs
from rewire.agents.trace import EventType, RunSummary, RunTrace, load_trace
from rewire.agents.workspace import Workspace
from rewire.analyzers import build_index
from rewire.changes import diff_specs, parse_spec_text
from rewire.core.config import LLMSettings
from rewire.core.errors import AgentError, ConfigurationError
from rewire.impact import analyse_impact
from rewire.llm.registry import build_provider, build_provider_for, credential_for

SPEC = (
    'openapi: "3.0.3"\ninfo: {{title: OpenAI API, version: "{v}"}}\n'
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
    return client.chat.completions.create(max_tokens=1)
"""


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="a"\nversion="1"\ndependencies=["openai"]\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(CLIENT, encoding="utf-8")
    changes = diff_specs(
        parse_spec_text(SPEC.format(v="1", field="max_tokens")),
        parse_spec_text(SPEC.format(v="2", field="max_completion_tokens")),
    )
    index = build_index(tmp_path)
    workspace = Workspace.open(tmp_path)
    return ToolContext(
        workspace=workspace,
        index=index,
        changes=changes,
        impact=analyse_impact(changes, index),
        patch=PatchBuilder(read_file=workspace.read_full),
    )


# ------------------------------------------------------------------- tools ---


def test_every_tool_has_a_schema_and_handler() -> None:
    for tool in TOOLS:
        assert tool.spec.description
        assert tool.spec.parameters.get("type") == "object"
        assert callable(tool.handler)


def test_tool_order_is_stable() -> None:
    """Tool order forms part of the cached prompt prefix."""
    assert [spec.name for spec in tool_specs()] == [tool.name for tool in TOOLS]


def test_no_tool_can_execute_or_write() -> None:
    names = set(TOOLS_BY_NAME)
    assert not names & {"bash", "shell", "run", "write_file", "delete_file", "apply_patch"}


def test_list_files(context: ToolContext) -> None:
    assert "app.py" in invoke("list_files", context, {}).content


def test_read_file_is_line_numbered(context: ToolContext) -> None:
    result = invoke("read_file", context, {"path": "app.py", "start_line": 1, "limit": 2})
    assert "app.py lines 1-2 of" in result.content
    assert "    1| from openai import OpenAI" in result.content


def test_read_file_reports_truncation(context: ToolContext) -> None:
    result = invoke("read_file", context, {"path": "app.py", "limit": 1})
    assert "truncated" in result.content


def test_read_file_rejects_escaping_paths(context: ToolContext) -> None:
    result = invoke("read_file", context, {"path": "../../etc/passwd"})
    assert result.is_error
    assert "escapes the repository" in result.content


def test_search_code_classifies_usage(context: ToolContext) -> None:
    result = invoke("search_code", context, {"name": "max_tokens"})
    assert "keyword_argument" in result.content


def test_search_code_reports_nothing_found(context: ToolContext) -> None:
    assert "No references" in invoke("search_code", context, {"name": "absent"}).content


def test_find_calls_lists_keyword_arguments(context: ToolContext) -> None:
    result = invoke("find_calls", context, {"pattern": "chat.completions.create"})
    assert "keywords: max_tokens" in result.content


def test_find_calls_reports_nothing_found(context: ToolContext) -> None:
    assert "No calls" in invoke("find_calls", context, {"pattern": "nope"}).content


def test_find_symbol(context: ToolContext) -> None:
    assert "ask" in invoke("find_symbol", context, {"name": "ask"}).content
    assert "No symbol" in invoke("find_symbol", context, {"name": "absent"}).content


def test_inspect_api_change_shows_locations_and_confidence(context: ToolContext) -> None:
    result = invoke("inspect_api_change", context, {"field": "max_tokens"})
    assert "request_field_removed" in result.content
    assert "confidence" in result.content
    assert "replacement=max_completion_tokens" in result.content


def test_inspect_api_change_without_a_field_shows_everything(context: ToolContext) -> None:
    assert invoke("inspect_api_change", context, {}).content


def test_inspect_api_change_for_an_unknown_field(context: ToolContext) -> None:
    result = invoke("inspect_api_change", context, {"field": "nope"})
    assert "No detected API change" in result.content


def test_propose_edit_stages_without_writing(context: ToolContext) -> None:
    before = (context.workspace.root / "app.py").read_bytes()
    result = invoke(
        "propose_edit",
        context,
        {"file": "app.py", "old_text": "max_tokens=1", "new_text": "max_completion_tokens=1"},
    )
    assert not result.is_error
    assert "has not been written to disk" in result.content
    assert (context.workspace.root / "app.py").read_bytes() == before


def test_propose_edit_reports_ambiguity(context: ToolContext) -> None:
    result = invoke(
        "propose_edit", context, {"file": "app.py", "old_text": "client", "new_text": "c"}
    )
    assert result.is_error
    assert "ambiguous" in result.content


def test_show_diff_before_and_after_edits(context: ToolContext) -> None:
    assert "No edits" in invoke("show_diff", context, {}).content
    invoke(
        "propose_edit",
        context,
        {"file": "app.py", "old_text": "max_tokens=1", "new_text": "max_completion_tokens=1"},
    )
    assert "+++ b/app.py" in invoke("show_diff", context, {}).content


def test_unknown_tools_are_reported(context: ToolContext) -> None:
    result = invoke("rm", context, {})
    assert result.is_error
    assert "Unknown tool" in result.content


def test_bad_argument_types_are_reported(context: ToolContext) -> None:
    result = invoke("read_file", context, {"path": 42})
    assert result.is_error


def test_non_integer_line_numbers_fall_back(context: ToolContext) -> None:
    assert not invoke("read_file", context, {"path": "app.py", "start_line": "x"}).is_error


# ------------------------------------------------------------------- state ---


def test_every_state_has_declared_transitions() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(AgentState)


def test_terminal_states_have_no_exits() -> None:
    assert ALLOWED_TRANSITIONS[AgentState.CANDIDATE] == frozenset()
    assert ALLOWED_TRANSITIONS[AgentState.FAILED] == frozenset()
    assert is_terminal(AgentState.CANDIDATE)
    assert not is_terminal(AgentState.ANALYZE)


def test_a_candidate_cannot_be_reached_without_editing() -> None:
    """A patch has to be built before it can be proposed."""
    assert not can_transition(AgentState.ANALYZE, AgentState.CANDIDATE)
    assert not can_transition(AgentState.PLAN, AgentState.CANDIDATE)
    assert can_transition(AgentState.REVIEW, AgentState.CANDIDATE)


def test_any_state_can_fail() -> None:
    for state in (AgentState.ANALYZE, AgentState.PLAN, AgentState.EDIT, AgentState.REVIEW):
        assert can_transition(state, AgentState.FAILED)


# ------------------------------------------------------------------- trace ---


def test_events_are_sequenced(tmp_path: Path) -> None:
    with RunTrace("run", tmp_path) as trace:
        trace.record(EventType.RUN_STARTED, AgentState.ANALYZE)
        trace.record(EventType.RUN_FINISHED, AgentState.CANDIDATE)
    assert [event.sequence for event in trace.events] == [1, 2]


def test_events_are_flushed_immediately(tmp_path: Path) -> None:
    """A run killed mid-flight is exactly when its trace is wanted."""
    trace = RunTrace("run", tmp_path)
    trace.record(EventType.RUN_STARTED, AgentState.ANALYZE)
    assert (tmp_path / "trace.jsonl").read_text(encoding="utf-8").strip()
    trace.close()


def test_traces_without_a_directory_stay_in_memory() -> None:
    trace = RunTrace("run", None)
    trace.record(EventType.RUN_STARTED, AgentState.ANALYZE)
    assert len(trace.events) == 1
    assert trace.write_summary(RunSummary(run_id="run", repository="/x")) is None


def test_an_unwritable_trace_directory_is_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("a file", encoding="utf-8")
    with pytest.raises(AgentError, match="could not open run trace"):
        RunTrace("run", blocker)


def test_truncated_traces_are_still_readable(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text('{"not": "an event"}\n\nnot json at all\n', encoding="utf-8")
    assert load_trace(path) == []


def test_of_type_filters(tmp_path: Path) -> None:
    with RunTrace("run", tmp_path) as trace:
        trace.record(EventType.TOOL_CALL, AgentState.EDIT, tool="a")
        trace.record(EventType.TOOL_CALL, AgentState.EDIT, tool="b")
        trace.record(EventType.RUN_FINISHED, AgentState.CANDIDATE)
    assert len(trace.of_type(EventType.TOOL_CALL)) == 2


def test_produced_patch_is_named_for_what_it_means() -> None:
    assert RunSummary(run_id="r", repository="/x", final_state=AgentState.CANDIDATE).produced_patch
    assert not RunSummary(run_id="r", repository="/x", final_state=AgentState.FAILED).produced_patch


# ---------------------------------------------------------------- registry ---


def test_a_null_provider_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="no LLM provider"):
        build_provider(LLMSettings(provider="null"))


def test_a_provider_without_a_key_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="no API key"):
        build_provider(LLMSettings(provider="openai"))


def test_providers_are_built_from_settings() -> None:
    openai_provider = build_provider(
        LLMSettings(provider="openai", model="gpt-4o", openai_api_key="k")
    )
    assert openai_provider.name == "openai"
    assert openai_provider.model == "gpt-4o"

    anthropic_provider = build_provider(
        LLMSettings(provider="anthropic", model="claude-opus-5", anthropic_api_key="k")
    )
    assert anthropic_provider.name == "anthropic"


def test_openrouter_reuses_the_chat_completions_adapter() -> None:
    provider = build_provider(
        LLMSettings(provider="openrouter", model="anything", openrouter_api_key="k")
    )
    assert provider.name == "openai"


def test_a_credential_is_found_only_where_one_is_set() -> None:
    """The comparison harness asks this before spending an hour on a model."""
    settings = LLMSettings(provider="openai", openai_api_key="k")
    assert credential_for(settings, "openai") == "k"
    assert credential_for(settings, "anthropic") is None


def test_an_empty_credential_counts_as_absent() -> None:
    """An empty environment variable is a missing key, not a key of length zero."""
    assert credential_for(LLMSettings(openai_api_key=""), "openai") is None


def test_a_model_can_be_built_without_changing_the_configured_provider() -> None:
    """Comparing models must vary the model and nothing else."""
    settings = LLMSettings(
        provider="anthropic", model="claude-opus-5", openai_api_key="k", temperature=0.7
    )
    provider = build_provider_for(settings, provider="openai", model="gpt-4o-mini")
    assert (provider.name, provider.model) == ("openai", "gpt-4o-mini")
    assert provider.temperature == 0.7
    assert settings.provider == "anthropic"


def test_building_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="unknown provider"):
        build_provider_for(LLMSettings(openai_api_key="k"), provider="acme", model="x")
