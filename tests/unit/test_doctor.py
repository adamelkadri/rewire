"""Tests for the environment preflight checks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from rewire.core import doctor
from rewire.core.config import Settings
from rewire.core.doctor import CheckResult, CheckStatus, DoctorReport


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def stub_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/fake")


def test_python_version_check_passes_on_current_interpreter() -> None:
    result = doctor.check_python_version()
    assert result.status is CheckStatus.OK
    assert str(sys.version_info.major) in result.detail


def test_python_version_check_fails_when_too_old(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "MINIMUM_PYTHON", (99, 0))
    result = doctor.check_python_version()
    assert result.status is CheckStatus.FAIL
    assert result.remedy is not None


def test_missing_required_binary_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    result = doctor.check_git()
    assert result.status is CheckStatus.FAIL
    assert "not found on PATH" in result.detail


def test_missing_optional_binary_only_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    result = doctor.check_ripgrep()
    assert result.status is CheckStatus.WARN


def test_docker_daemon_unreachable_fails(monkeypatch: pytest.MonkeyPatch, stub_which: None) -> None:
    monkeypatch.setattr(
        doctor,
        "_run",
        lambda _cmd: _completed(returncode=1, stderr="Cannot connect to the Docker daemon\n"),
    )
    result = doctor.check_docker()
    assert result.status is CheckStatus.FAIL
    assert "Cannot connect to the Docker daemon" in result.detail


def test_probe_timeout_is_reported(monkeypatch: pytest.MonkeyPatch, stub_which: None) -> None:
    def _raise(_cmd: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="docker", timeout=doctor.PROBE_TIMEOUT_SECONDS)

    monkeypatch.setattr(doctor, "_run", _raise)
    result = doctor.check_docker()
    assert result.status is CheckStatus.FAIL
    assert "timed out" in result.detail


def test_successful_probe_reports_version(
    monkeypatch: pytest.MonkeyPatch, stub_which: None
) -> None:
    monkeypatch.setattr(doctor, "_run", lambda _cmd: _completed(stdout="git version 2.51.0\n"))
    result = doctor.check_git()
    assert result.status is CheckStatus.OK
    assert result.detail == "git version 2.51.0"


def test_llm_check_warns_when_no_provider_configured(settings: Settings) -> None:
    result = doctor.check_llm_credentials(settings)
    assert result.status is CheckStatus.WARN


def test_llm_check_fails_when_provider_lacks_key(tmp_path: Path) -> None:
    configured = Settings(data_dir=tmp_path, llm={"provider": "anthropic"}, _env_file=None)
    result = doctor.check_llm_credentials(configured)
    assert result.status is CheckStatus.FAIL


def test_llm_check_passes_with_key(tmp_path: Path) -> None:
    configured = Settings(
        data_dir=tmp_path,
        llm={"provider": "anthropic", "anthropic_api_key": "sk-ant-x"},
        _env_file=None,
    )
    result = doctor.check_llm_credentials(configured)
    assert result.status is CheckStatus.OK
    assert "sk-ant-x" not in result.detail


def test_data_dir_check_creates_and_probes(settings: Settings) -> None:
    result = doctor.check_data_dir(settings)
    assert result.status is CheckStatus.OK
    assert settings.data_dir.is_dir()
    assert not (settings.data_dir / ".write-probe").exists()


def test_data_dir_check_fails_when_unwritable(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    result = doctor.check_data_dir(Settings(data_dir=blocker, _env_file=None))
    assert result.status is CheckStatus.FAIL


def test_run_checks_covers_every_dependency(settings: Settings) -> None:
    report = doctor.run_checks(settings)
    assert {result.name for result in report.results} == {
        "python",
        "git",
        "docker",
        "ripgrep",
        "llm",
        "data-dir",
    }


def test_report_ok_ignores_warnings() -> None:
    report = DoctorReport(
        results=[
            CheckResult(name="a", status=CheckStatus.OK, detail=""),
            CheckResult(name="b", status=CheckStatus.WARN, detail=""),
        ]
    )
    assert report.ok is True


def test_report_not_ok_when_a_check_fails() -> None:
    report = DoctorReport(
        results=[
            CheckResult(name="a", status=CheckStatus.OK, detail=""),
            CheckResult(name="b", status=CheckStatus.FAIL, detail=""),
        ]
    )
    assert report.ok is False
    assert report.counts()[CheckStatus.FAIL] == 1


def test_probe_reports_os_error(monkeypatch: pytest.MonkeyPatch, stub_which: None) -> None:
    def _raise(_cmd: list[str]) -> subprocess.CompletedProcess[str]:
        raise PermissionError("permission denied")

    monkeypatch.setattr(doctor, "_run", _raise)
    result = doctor.check_git()
    assert result.status is CheckStatus.FAIL
    assert "could not be executed" in result.detail


def test_run_executes_a_real_command() -> None:
    """The probe helper must actually shell out, not just be mocked in tests."""
    completed = doctor._run([sys.executable, "-c", "print('hello')"])
    assert completed.returncode == 0
    assert completed.stdout.strip() == "hello"
