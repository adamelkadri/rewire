"""Coverage for the remaining agent and provider edge paths."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import openai
import pytest

from rewire.agents.migration_agent import _accumulate_cost
from rewire.agents.patch import PatchBuilder
from rewire.agents.prompts import build_review_nudge, build_task_prompt
from rewire.agents.state import AgentState
from rewire.agents.tools import ToolContext, invoke
from rewire.agents.trace import RunTrace
from rewire.agents.workspace import Workspace
from rewire.analyzers import build_index
from rewire.changes import diff_specs, parse_spec_text
from rewire.core.errors import AgentError, LLMError
from rewire.impact import analyse_impact
from rewire.impact.models import ImpactReport
from rewire.llm.base import wrap_provider_error
from rewire.llm.models import LLMResponse
from rewire.llm.scripted import ScriptedProvider

EMPTY_SPEC = 'openapi: "3.0.3"\ninfo: {title: T, version: "1"}\npaths: {}\n'


def http_response(status: int) -> httpx2.Response:
    return httpx2.Response(status, request=httpx2.Request("POST", "https://api.test/v1"))


# ------------------------------------------------------------------ prompts --


def test_task_prompt_when_nothing_is_affected() -> None:
    changes = diff_specs(parse_spec_text(EMPTY_SPEC), parse_spec_text(EMPTY_SPEC))
    prompt = build_task_prompt(changes, ImpactReport(repository="/x"))
    assert "No affected code was found" in prompt


def test_task_prompt_notes_changes_it_did_not_show(tmp_path: Path) -> None:
    spec = (
        'openapi: "3.0.3"\ninfo: {{title: OpenAI API, version: "{v}"}}\n'
        + """paths:
  /v1/a:
    post:
      requestBody:
        content:
          application/json:
            schema: {{type: object, properties: {{{f}: {{type: integer}}}}}}
      responses: {{'200': {{description: OK}}}}
  /v1/b:
    post:
      requestBody:
        content:
          application/json:
            schema: {{type: object, properties: {{{g}: {{type: integer}}}}}}
      responses: {{'200': {{description: OK}}}}
"""
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="a"\nversion="1"\ndependencies=["openai"]\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        "import openai\n\nP = {'alpha': 1, 'beta': 2}\n", encoding="utf-8"
    )
    changes = diff_specs(
        parse_spec_text(spec.format(v="1", f="alpha", g="beta")),
        parse_spec_text(spec.format(v="2", f="alpha2", g="beta2")),
    )
    index = build_index(tmp_path)
    impact = analyse_impact(changes, index)
    prompt = build_task_prompt(changes, impact, max_changes=1)
    assert "further affected change(s) not shown" in prompt


def test_review_nudges_differ_by_progress() -> None:
    assert "have not staged any edit" in build_review_nudge([])
    assert "have staged edits" in build_review_nudge(["a.py"])


# -------------------------------------------------------------------- tools --


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    from rewire.agents.patch import PatchBuilder

    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    changes = diff_specs(parse_spec_text(EMPTY_SPEC), parse_spec_text(EMPTY_SPEC))
    index = build_index(tmp_path)
    workspace = Workspace.open(tmp_path)
    return ToolContext(
        workspace=workspace,
        index=index,
        changes=changes,
        impact=analyse_impact(changes, index),
        patch=PatchBuilder(read_file=workspace.read_full),
    )


def test_listing_an_empty_directory(context: ToolContext, tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert "No files under" in invoke("list_files", context, {"directory": "empty"}).content


def test_an_unexpected_domain_error_becomes_a_tool_error(
    context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rewire.core.errors import EvaluationError

    def _explode(_context: ToolContext, _arguments: dict[str, object]) -> str:
        raise EvaluationError("unexpected")

    monkeypatch.setattr(
        "rewire.agents.tools.TOOLS_BY_NAME", {"boom": SimpleNamespace(handler=_explode)}
    )
    result = invoke("boom", context, {})
    assert result.is_error
    assert "evaluation_error" in result.content


# -------------------------------------------------------------- workspace ----


def test_unreadable_files_are_reported(tmp_path: Path) -> None:
    from rewire.core.errors import RepositoryError

    target = tmp_path / "locked.py"
    target.write_text("x = 1\n", encoding="utf-8")
    target.chmod(0o000)
    try:
        with pytest.raises(RepositoryError, match="could not read"):
            Workspace.open(tmp_path).read("locked.py")
    finally:
        target.chmod(0o644)


def test_listing_tolerates_unreadable_directories(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        assert Workspace.open(tmp_path).list_files() == ["ok.py"]
    finally:
        locked.chmod(0o755)


def test_listing_skips_symlinks(tmp_path: Path) -> None:
    (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    assert Workspace.open(tmp_path).list_files() == ["real.py"]


# ------------------------------------------------------------------- agent ----


def test_illegal_transitions_are_a_bug_not_a_recovery(tmp_path: Path) -> None:
    """A model cannot cause one, so it raises rather than being smoothed over."""
    from rewire.agents.migration_agent import MigrationAgent

    with RunTrace("r", None) as trace, pytest.raises(AgentError, match="illegal agent state"):
        MigrationAgent._transition(trace, AgentState.ANALYZE, AgentState.CANDIDATE)


def test_transitioning_to_the_same_state_is_a_no_op() -> None:
    from rewire.agents.migration_agent import MigrationAgent

    with RunTrace("r", None) as trace:
        assert (
            MigrationAgent._transition(trace, AgentState.EDIT, AgentState.EDIT) is AgentState.EDIT
        )


def test_terminal_states_absorb_further_transitions() -> None:
    from rewire.agents.migration_agent import MigrationAgent

    with RunTrace("r", None) as trace:
        assert (
            MigrationAgent._transition(trace, AgentState.FAILED, AgentState.EDIT)
            is AgentState.FAILED
        )


def test_unknown_cost_propagates_rather_than_resetting() -> None:
    """One unpriced response must make the whole run's cost unknown, not cheap."""
    assert _accumulate_cost(1.0, LLMResponse(cost_usd=0.5)) == pytest.approx(1.5)
    assert _accumulate_cost(1.0, LLMResponse(cost_usd=None)) is None
    assert _accumulate_cost(None, LLMResponse(cost_usd=0.5)) is None


# ---------------------------------------------------------------- providers ---


def test_scripted_provider_exhaustion_is_an_error() -> None:
    provider = ScriptedProvider([])
    with pytest.raises(LLMError, match="ran out of responses"):
        provider.complete(system="", messages=[])


def test_scripted_provider_records_requests() -> None:
    provider = ScriptedProvider([LLMResponse(text="hi")])
    assert provider.last_request is None
    provider.complete(system="s", messages=[])
    assert provider.last_request is not None
    assert provider.last_request.system == "s"


def test_anthropic_status_errors_map_to_domain_errors() -> None:
    from rewire.llm.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(model="claude-opus-5", api_key="k")

    def _raise(**_kwargs: object) -> None:
        raise anthropic.APIStatusError("bad", response=http_response(400), body=None)

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=_raise))  # type: ignore[assignment]
    with pytest.raises(LLMError, match="anthropic returned 400"):
        provider.complete(system="", messages=[])


def test_anthropic_connection_errors_map_to_domain_errors() -> None:
    from rewire.llm.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(model="claude-opus-5", api_key="k")

    def _raise(**_kwargs: object) -> None:
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.test"))

    provider._client = SimpleNamespace(messages=SimpleNamespace(create=_raise))  # type: ignore[assignment]
    with pytest.raises(LLMError, match="request failed"):
        provider.complete(system="", messages=[])


def test_openai_generic_errors_map_to_domain_errors() -> None:
    from rewire.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider(model="gpt-4o", api_key="k")

    def _raise(**_kwargs: object) -> None:
        raise openai.OpenAIError("something broke")

    provider._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=_raise))
    )
    with pytest.raises(LLMError, match="request failed"):
        provider.complete(system="", messages=[])


def test_wrapped_errors_name_the_provider_not_the_request() -> None:
    """The message must come from the exception, never from request contents."""
    error = wrap_provider_error("openai", ValueError("boom"))
    assert error.details["provider"] == "openai"
    assert "boom" in str(error)


def test_the_prompt_names_the_enum_values_that_changed(tmp_path: Path) -> None:
    """The failure `05-enum-value-removed` reproduced sixteen times.

    Told only that a value was removed and another added at the same field, the
    agent has no way to learn what to migrate to and invents a value present in
    neither specification. Both values were on the change all along.
    """
    spec = (
        'openapi: "3.0.3"\ninfo: {{title: OpenAI API, version: "{v}"}}\n'
        + """paths:
  /v1/chat/completions:
    post:
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                response_format: {{type: string, enum: [text, {e}, srt]}}
      responses: {{'200': {{description: OK}}}}
"""
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="a"\nversion="1"\ndependencies=["openai"]\n', encoding="utf-8"
    )
    (tmp_path / "app.py").write_text(
        "import openai\n\nBODY = {'response_format': 'json'}\n", encoding="utf-8"
    )
    changes = diff_specs(
        parse_spec_text(spec.format(v="1", e="json")),
        parse_spec_text(spec.format(v="2", e="json_object")),
    )
    impact = analyse_impact(changes, build_index(tmp_path))

    prompt = build_task_prompt(changes, impact)
    assert "was: 'json'" in prompt
    assert "now: 'json_object'" in prompt

    # And through the tool, which an agent that withholds locations still has.
    workspace = Workspace.open(tmp_path)
    context = ToolContext(
        workspace=workspace,
        index=build_index(tmp_path),
        impact=impact,
        changes=changes,
        patch=PatchBuilder(read_file=workspace.read_full),
    )
    shown = invoke("inspect_api_change", context, {"field": "response_format"})
    assert "was: 'json'" in shown.content
    assert "now: 'json_object'" in shown.content
