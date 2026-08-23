"""Tests for the `rewire propose` command.

The provider is replaced with a scripted one, so these exercise the real command
end to end — deterministic pipeline, agent loop, rendering — without a network
call or an API key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rewire.cli import app
from rewire.core.errors import ConfigurationError
from rewire.llm import ScriptBuilder, ScriptedProvider

runner = CliRunner()

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
    return client.chat.completions.create(max_tokens=100)
"""


@pytest.fixture
def case(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname="a"\nversion="1"\ndependencies=["openai"]\n', encoding="utf-8"
    )
    (repo / "app.py").write_text(CLIENT, encoding="utf-8")
    (tmp_path / "old.yaml").write_text(SPEC.format(v="1", field="max_tokens"), encoding="utf-8")
    (tmp_path / "new.yaml").write_text(
        SPEC.format(v="2", field="max_completion_tokens"), encoding="utf-8"
    )
    return tmp_path


def use_provider(monkeypatch: pytest.MonkeyPatch, provider: ScriptedProvider) -> None:
    monkeypatch.setattr("rewire.cli.build_provider", lambda _settings: provider)


def editing_script() -> ScriptedProvider:
    return (
        ScriptBuilder()
        .calls(
            "propose_edit",
            file="app.py",
            old_text="max_tokens=100",
            new_text="max_completion_tokens=100",
            rationale="renamed",
        )
        .says("Renamed the request field.")
        .build()
    )


def invoke(case: Path, *extra: str) -> object:
    return runner.invoke(
        app,
        [
            "propose",
            str(case / "repo"),
            "--old",
            str(case / "old.yaml"),
            "--new",
            str(case / "new.yaml"),
            *extra,
        ],
    )


def test_a_successful_proposal_is_reported(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_provider(monkeypatch, editing_script())
    result = invoke(case)
    assert result.exit_code == 0
    assert "CANDIDATE" in result.stdout
    assert "app.py" in result.stdout


def test_the_diff_is_shown_by_default(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_provider(monkeypatch, editing_script())
    assert "max_completion_tokens=100" in invoke(case).stdout


def test_the_diff_can_be_suppressed(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_provider(monkeypatch, editing_script())
    assert "+++ b/app.py" not in invoke(case, "--no-diff").stdout


def test_the_output_never_claims_verification(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reader must not be able to mistake a proposal for a working patch."""
    use_provider(monkeypatch, editing_script())
    stdout = invoke(case).stdout
    assert "This patch is a proposal" in stdout
    assert "no tests were run" in stdout


def test_the_agent_summary_is_shown(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_provider(monkeypatch, editing_script())
    assert "Renamed the request field." in invoke(case).stdout


def test_the_diff_can_be_written_to_a_file(
    case: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_provider(monkeypatch, editing_script())
    target = tmp_path / "out.patch"
    invoke(case, "--write-diff", str(target))
    assert "max_completion_tokens" in target.read_text(encoding="utf-8")


def test_the_repository_is_left_untouched(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_provider(monkeypatch, editing_script())
    before = (case / "repo" / "app.py").read_bytes()
    invoke(case)
    assert (case / "repo" / "app.py").read_bytes() == before


def test_a_run_without_a_patch_exits_non_zero(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_provider(monkeypatch, ScriptBuilder().says("nothing").says("still nothing").build())
    result = invoke(case)
    assert result.exit_code == 1
    assert "NO PATCH" in result.stdout


def test_an_unaffected_repository_short_circuits(
    case: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent must not be called at all when there is nothing to migrate."""
    provider = editing_script()
    use_provider(monkeypatch, provider)
    (case / "repo" / "app.py").write_text("x = 1\n", encoding="utf-8")

    result = invoke(case)
    assert result.exit_code == 0
    assert "nothing to migrate" in result.stdout
    assert provider.call_count == 0


def test_a_missing_provider_is_reported(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _unconfigured(_settings: object) -> None:
        raise ConfigurationError("no LLM provider is configured")

    monkeypatch.setattr("rewire.cli.build_provider", _unconfigured)
    with pytest.raises(ConfigurationError):
        runner.invoke(
            app,
            [
                "propose",
                str(case / "repo"),
                "--old",
                str(case / "old.yaml"),
                "--new",
                str(case / "new.yaml"),
            ],
            catch_exceptions=False,
        )


def test_budgets_are_passed_through(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ScriptBuilder().calls("list_files").build(repeat_last=True)
    use_provider(monkeypatch, provider)
    result = invoke(case, "--max-iterations", "2")
    assert result.exit_code == 1
    assert provider.call_count == 2


def test_a_trace_directory_is_reported(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_provider(monkeypatch, editing_script())
    assert "trace:" in invoke(case).stdout
