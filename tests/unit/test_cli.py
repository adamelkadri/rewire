"""Tests for the CLI surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rewire.__version__ import __version__
from rewire.cli import app, main
from rewire.core import doctor
from rewire.core.config import LogLevel
from rewire.core.doctor import CheckResult, CheckStatus, DoctorReport
from rewire.core.errors import GitError, SpecParseError

runner = CliRunner()


def test_help_lists_implemented_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.stdout
    assert "config" in result.stdout


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code != 0
    assert "Usage" in result.stdout


def _patch_report(monkeypatch: pytest.MonkeyPatch, *statuses: CheckStatus) -> None:
    report = DoctorReport(
        results=[
            CheckResult(name=f"check-{i}", status=status, detail="detail", remedy="do a thing")
            for i, status in enumerate(statuses)
        ]
    )
    monkeypatch.setattr("rewire.cli.run_checks", lambda _settings: report)


def test_doctor_exits_zero_when_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_report(monkeypatch, CheckStatus.OK, CheckStatus.WARN)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0


def test_doctor_exits_nonzero_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_report(monkeypatch, CheckStatus.OK, CheckStatus.FAIL)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1


def test_doctor_json_output_is_parseable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_report(monkeypatch, CheckStatus.OK)
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["results"][0]["status"] == "ok"


def test_doctor_runs_against_the_real_environment() -> None:
    """The command must work end to end, not only against a stubbed report."""
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code in (0, 1)
    assert "Rewire environment" in result.stdout


def test_config_command_redacts_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REWIRE_LLM__ANTHROPIC_API_KEY", "sk-ant-topsecret")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "topsecret" not in result.stdout
    assert json.loads(result.stdout)["environment"] == "local"


def test_probe_timeout_constant_is_bounded() -> None:
    """A hung Docker daemon must not hang the CLI indefinitely."""
    assert 0 < doctor.PROBE_TIMEOUT_SECONDS <= 30


def test_verbose_flag_enables_debug_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[object] = []
    monkeypatch.setattr("rewire.cli.configure_from_settings", captured.append)
    _patch_report(monkeypatch, CheckStatus.OK)

    assert runner.invoke(app, ["--verbose", "doctor"]).exit_code == 0
    assert captured[-1].log_level is LogLevel.DEBUG  # type: ignore[attr-defined]


def test_main_reports_domain_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom() -> None:
        # Square brackets in the message would be swallowed as Rich markup.
        raise GitError("detached head at [abc123]", branch="main")

    monkeypatch.setattr("rewire.cli.app", _boom)

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 1
    stderr = capsys.readouterr().err
    assert "git_error" in stderr
    assert "detached head at [abc123]" in stderr
    assert "Traceback" not in stderr


def test_doctor_table_does_not_swallow_bracketed_text(monkeypatch: pytest.MonkeyPatch) -> None:
    report = DoctorReport(
        results=[
            CheckResult(
                name="docker",
                status=CheckStatus.FAIL,
                detail="cannot run [docker version]",
                remedy="start the [daemon]",
            )
        ]
    )
    monkeypatch.setattr("rewire.cli.run_checks", lambda _settings: report)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "[docker version]" in result.stdout
    assert "[daemon]" in result.stdout


# ------------------------------------------------------------------ api-diff --


def test_api_diff_renders_a_table(specs: Path) -> None:
    result = runner.invoke(
        app, ["api-diff", str(specs / "openai/chat_old.yaml"), str(specs / "openai/chat_new.yaml")]
    )
    assert result.exit_code == 0
    # Changes are grouped under the endpoint they affect.
    assert "POST /v1/chat/completions" in result.stdout
    assert "max_tokens" in result.stdout
    assert "breaking" in result.stdout


def test_api_diff_table_shows_rename_targets(specs: Path) -> None:
    """The removal/replacement link is the actionable part; it must be visible."""
    result = runner.invoke(
        app, ["api-diff", str(specs / "openai/chat_old.yaml"), str(specs / "openai/chat_new.yaml")]
    )
    assert "->" in result.stdout
    assert "max_completion_tokens" in result.stdout


def test_api_diff_json_output_is_parseable(specs: Path) -> None:
    result = runner.invoke(
        app,
        [
            "api-diff",
            str(specs / "anthropic/messages_old.json"),
            str(specs / "anthropic/messages_new.json"),
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["summary"]["breaking"] >= 1
    assert {c["type"] for c in payload["changes"]}
    assert all("severity" in change for change in payload["changes"])


def test_api_diff_reports_no_changes_for_identical_specs(specs: Path) -> None:
    spec = str(specs / "synthetic/minimal_old.yaml")
    result = runner.invoke(app, ["api-diff", spec, spec])
    assert result.exit_code == 0
    assert "No changes detected" in result.stdout


def test_api_diff_min_severity_filters_output(specs: Path) -> None:
    args = ["api-diff", str(specs / "openai/chat_old.yaml"), str(specs / "openai/chat_new.yaml")]
    unfiltered = json.loads(runner.invoke(app, [*args, "--json"]).stdout)
    filtered = json.loads(
        runner.invoke(app, [*args, "--json", "--min-severity", "breaking"]).stdout
    )
    assert len(filtered["changes"]) < len(unfiltered["changes"])
    assert {c["severity"] for c in filtered["changes"]} == {"breaking"}
    # The summary always describes the whole diff, not the filtered view.
    assert filtered["summary"]["total"] == unfiltered["summary"]["total"]


@pytest.mark.parametrize(
    ("fail_on", "expected"),
    [("never", 0), ("breaking", 1), ("potentially-breaking", 1), ("any", 1)],
)
def test_api_diff_fail_on_thresholds(specs: Path, fail_on: str, expected: int) -> None:
    result = runner.invoke(
        app,
        [
            "api-diff",
            str(specs / "openai/chat_old.yaml"),
            str(specs / "openai/chat_new.yaml"),
            "--fail-on",
            fail_on,
        ],
    )
    assert result.exit_code == expected


def test_api_diff_fail_on_does_not_fire_without_changes(specs: Path) -> None:
    spec = str(specs / "synthetic/minimal_old.yaml")
    assert runner.invoke(app, ["api-diff", spec, spec, "--fail-on", "any"]).exit_code == 0


def test_api_diff_rejects_a_missing_file(specs: Path) -> None:
    result = runner.invoke(
        app, ["api-diff", str(specs / "nope.yaml"), str(specs / "synthetic/minimal_new.yaml")]
    )
    assert result.exit_code != 0


def test_api_diff_surfaces_spec_errors_without_a_traceback(specs: Path) -> None:
    """An unsupported spec must produce a readable message, not a stack trace."""
    with pytest.raises(SpecParseError, match=r"Swagger 2\.0"):
        runner.invoke(
            app,
            [
                "api-diff",
                str(specs / "invalid/swagger2.yaml"),
                str(specs / "synthetic/minimal_new.yaml"),
            ],
            catch_exceptions=False,
        )


def test_api_diff_output_is_deterministic(specs: Path) -> None:
    args = [
        "api-diff",
        str(specs / "stripe/charges_old.yaml"),
        str(specs / "stripe/charges_new.yaml"),
        "--json",
    ]
    assert runner.invoke(app, args).stdout == runner.invoke(app, args).stdout
