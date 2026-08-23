"""Tests for `rewire verify` and `propose --verify`.

The sandbox itself is replaced with a scripted runner, so these exercise the
real commands — detection, orchestration, rendering, exit codes — without a
Docker daemon.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rewire.cli import app
from rewire.sandbox.models import VerificationRequest
from rewire.sandbox.scripted import ScriptedRunner
from rewire.sandbox.verifier import verify as real_verify

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


@pytest.fixture(autouse=True)
def _quiet_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWIRE_LOG_LEVEL", "WARNING")


@pytest.fixture
def case(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nname="a"\nversion="1"\ndependencies=["openai"]\n[tool.ruff]\n',
        encoding="utf-8",
    )
    (repo / "app.py").write_text(CLIENT, encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (tmp_path / "old.yaml").write_text(SPEC.format(v="1", field="max_tokens"), encoding="utf-8")
    (tmp_path / "new.yaml").write_text(
        SPEC.format(v="2", field="max_completion_tokens"), encoding="utf-8"
    )
    return tmp_path


def use_sandbox(monkeypatch: pytest.MonkeyPatch, sandbox: ScriptedRunner) -> None:
    """Replace the Docker backend with a scripted one, keeping everything else real."""

    def patched(repository: Path, patch: object = None, **kwargs: object) -> object:
        kwargs.pop("runner_factory", None)
        return real_verify(
            repository,
            patch,  # type: ignore[arg-type]
            runner_factory=lambda _root, _request: sandbox,
            **kwargs,  # type: ignore[arg-type]
        )

    monkeypatch.setattr("rewire.cli.verify", patched)


# ----------------------------------------------------------------- verify ---


def test_a_baseline_run_reports_every_check(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_sandbox(monkeypatch, ScriptedRunner())
    result = runner.invoke(app, ["verify", str(case / "repo")])
    assert result.exit_code == 0
    for expected in ("pytest", "ruff", "compileall", "mypy"):
        assert expected in result.stdout


def test_a_baseline_run_says_it_proves_nothing_about_a_patch(
    case: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sandbox(monkeypatch, ScriptedRunner())
    result = runner.invoke(app, ["verify", str(case / "repo")])
    assert "INCONCLUSIVE" in result.stdout
    assert "no patch was supplied" in result.stdout


def test_checks_the_repository_does_not_configure_are_shown_as_skipped(
    case: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report that omits what it did not measure reads as fuller than it is."""
    use_sandbox(monkeypatch, ScriptedRunner())
    result = runner.invoke(app, ["verify", str(case / "repo")])
    assert "skipped" in result.stdout


def test_the_report_states_the_network_policy(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_sandbox(monkeypatch, ScriptedRunner())
    assert "no network" in runner.invoke(app, ["verify", str(case / "repo")]).stdout


def test_a_failing_check_shows_its_output(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = ScriptedRunner().when("pip install", exit_code=1, stderr="ERROR: no matching wheel")
    use_sandbox(monkeypatch, sandbox)
    result = runner.invoke(app, ["verify", str(case / "repo")])
    assert "Dependency installation failed" in result.stdout
    assert "no matching wheel" in result.stdout


def test_installation_can_be_skipped(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = ScriptedRunner()
    use_sandbox(monkeypatch, sandbox)
    runner.invoke(app, ["verify", str(case / "repo"), "--no-install"])
    assert all(call.network == "none" for call in sandbox.calls)


def test_the_image_can_be_overridden(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_sandbox(monkeypatch, ScriptedRunner())
    result = runner.invoke(app, ["verify", str(case / "repo"), "--image", "python:3.13-slim"])
    assert "python:3.13-slim" in result.stdout


def test_the_timeout_can_be_overridden(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = ScriptedRunner()
    use_sandbox(monkeypatch, sandbox)
    runner.invoke(app, ["verify", str(case / "repo"), "--timeout", "11"])
    assert 11 in {call.timeout for call in sandbox.calls}


def test_settings_supply_the_sandbox_policy(case: Path) -> None:
    """The CLI defaults come from configuration, not from hard-coded numbers."""
    from rewire.cli import _verification_request
    from rewire.core.config import Settings

    settings = Settings(sandbox={"memory_limit_mb": 777, "image": "custom:tag"})
    request = _verification_request(settings, image=None, timeout=None)
    assert request == VerificationRequest(
        image="custom:tag",
        check_timeout_seconds=settings.sandbox.timeout_seconds,
        memory_limit_mb=777,
        cpu_limit=settings.sandbox.cpu_limit,
        pids_limit=settings.sandbox.pids_limit,
        read_only_rootfs=settings.sandbox.read_only_rootfs,
        max_repo_size_mb=settings.sandbox.max_repo_size_mb,
    )


# --------------------------------------------------------- propose --verify ---


def propose(case: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> object:
    from rewire.llm import ScriptBuilder

    provider = (
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
    monkeypatch.setattr("rewire.cli.build_provider", lambda _settings: provider)
    return runner.invoke(
        app,
        [
            "propose",
            str(case / "repo"),
            "--old",
            str(case / "old.yaml"),
            "--new",
            str(case / "new.yaml"),
            "--no-diff",
            *extra,
        ],
    )


def test_a_proposal_is_not_verified_unless_asked(
    case: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = ScriptedRunner()
    use_sandbox(monkeypatch, sandbox)
    result = propose(case, monkeypatch)
    assert result.exit_code == 0
    assert sandbox.calls == []
    assert "This patch is a proposal" in result.stdout


def test_a_verified_proposal_reports_the_evidence(
    case: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_sandbox(monkeypatch, ScriptedRunner())
    result = propose(case, monkeypatch, "--verify")
    assert result.exit_code == 0
    assert "VERIFIED" in result.stdout
    assert "test suite passed after the patch" in result.stdout
    assert "This patch is a proposal" not in result.stdout


def test_a_regressing_proposal_fails_the_command(
    case: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A patch that breaks the build must not exit zero, or CI would accept it."""
    sandbox = ScriptedRunner().when("-m pytest", exit_code=0, times=1)
    sandbox.when("-m pytest", exit_code=1, stdout="1 failed, 0 passed")
    use_sandbox(monkeypatch, sandbox)
    result = propose(case, monkeypatch, "--verify")
    assert result.exit_code == 1
    assert "REGRESSED" in result.stdout
    assert "1 failed" in result.stdout


def test_an_inconclusive_proposal_also_fails_the_command(
    case: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence of evidence is not success; only VERIFIED exits zero."""
    sandbox = ScriptedRunner().when("-m pytest", exit_code=127, stderr="No module named pytest")
    use_sandbox(monkeypatch, sandbox)
    result = propose(case, monkeypatch, "--verify")
    assert result.exit_code == 1
    assert "INCONCLUSIVE" in result.stdout
