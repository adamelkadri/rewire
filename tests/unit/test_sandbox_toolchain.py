"""Tests for check detection, staging and the result types."""

from __future__ import annotations

from pathlib import Path

import pytest

from rewire.core.errors import SandboxError
from rewire.sandbox.models import (
    CheckKind,
    CheckResult,
    CheckStatus,
    CommandOutcome,
    Verdict,
    truncate_output,
)
from rewire.sandbox.staging import STAGING_EXCLUDED, stage_repository
from rewire.sandbox.toolchain import (
    CONTAINER_PATH,
    VENV_DIR,
    detect_install,
    has_tests,
    plan_checks,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    return root


def _write_pyproject(root: Path, body: str = "") -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "demo"\nversion = "1"\n{body}', encoding="utf-8"
    )


# --------------------------------------------------------------- detection ---


def test_syntax_is_always_checked(repo: Path) -> None:
    """A repository with no configuration at all still produces evidence."""
    plan = plan_checks(repo)
    assert CheckKind.SYNTAX in plan.kinds


def test_absent_tools_are_recorded_as_skipped_not_failed(repo: Path) -> None:
    """The difference between 'no linter' and 'linter failed' is the whole point."""
    plan = plan_checks(repo)
    skipped = {result.kind: result for result in plan.skipped}
    assert skipped[CheckKind.LINT].status is CheckStatus.SKIPPED
    assert skipped[CheckKind.TYPECHECK].status is CheckStatus.SKIPPED
    assert "does not configure" in skipped[CheckKind.LINT].reason


def test_tests_are_detected_from_a_test_directory(repo: Path) -> None:
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_x(): pass\n", encoding="utf-8")
    assert has_tests(repo)
    assert CheckKind.TESTS in plan_checks(repo).kinds


def test_an_empty_tests_directory_is_not_a_test_suite(repo: Path) -> None:
    (repo / "tests").mkdir()
    (repo / "tests" / "conftest.py").write_text("", encoding="utf-8")
    assert not has_tests(repo)


@pytest.mark.parametrize("name", ["test_root.py", "root_test.py"])
def test_top_level_test_modules_count(repo: Path, name: str) -> None:
    (repo / name).write_text("def test_x(): pass\n", encoding="utf-8")
    assert has_tests(repo)


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        ("[tool.ruff]\nline-length = 100\n", CheckKind.LINT),
        ("[tool.mypy]\nstrict = true\n", CheckKind.TYPECHECK),
    ],
)
def test_tools_configured_in_pyproject_are_run(repo: Path, body: str, kind: CheckKind) -> None:
    _write_pyproject(repo, body)
    assert kind in plan_checks(repo).kinds


@pytest.mark.parametrize("name", ["ruff.toml", ".ruff.toml"])
def test_standalone_ruff_config_is_found(repo: Path, name: str) -> None:
    (repo / name).write_text("line-length = 100\n", encoding="utf-8")
    assert CheckKind.LINT in plan_checks(repo).kinds


@pytest.mark.parametrize("name", ["mypy.ini", ".mypy.ini"])
def test_standalone_mypy_config_is_found(repo: Path, name: str) -> None:
    (repo / name).write_text("[mypy]\nstrict = True\n", encoding="utf-8")
    assert CheckKind.TYPECHECK in plan_checks(repo).kinds


def test_a_malformed_pyproject_does_not_abort_detection(repo: Path) -> None:
    """A broken manifest is the repository's problem; the other checks still run."""
    (repo / "pyproject.toml").write_text("this is not toml [[[", encoding="utf-8")
    assert CheckKind.SYNTAX in plan_checks(repo).kinds


def test_an_unreadable_pyproject_does_not_abort_detection(repo: Path) -> None:
    (repo / "pyproject.toml").write_bytes(b"\xff\xfe not utf-8")
    assert CheckKind.SYNTAX in plan_checks(repo).kinds


# ----------------------------------------------------------------- install ---


def test_a_repository_with_nothing_to_install_stays_offline(repo: Path) -> None:
    """No dependencies means the sandbox never needs the network at all."""
    assert detect_install(repo, frozenset({CheckKind.SYNTAX})) == ()


def test_a_packaged_project_is_installed_editable(repo: Path) -> None:
    _write_pyproject(repo)
    commands = [step.command for step in detect_install(repo, frozenset({CheckKind.TESTS}))]
    assert commands[0] == ("python3", "-m", "venv", VENV_DIR)
    assert "--editable" in commands[1]
    assert "pytest" in commands[1]


def test_requirements_are_installed_when_there_is_no_manifest(repo: Path) -> None:
    (repo / "requirements.txt").write_text("httpx\n", encoding="utf-8")
    install = detect_install(repo, frozenset())
    assert "--requirement" in install[1].command


def test_only_the_tools_the_plan_needs_are_installed(repo: Path) -> None:
    _write_pyproject(repo)
    install = detect_install(repo, frozenset({CheckKind.LINT}))
    assert "ruff" in install[1].command
    assert "mypy" not in install[1].command


def test_the_venv_precedes_the_image_interpreter_on_path() -> None:
    """Otherwise a check would silently run against the image's Python."""
    assert CONTAINER_PATH.startswith(f"{VENV_DIR}/bin:")


# ----------------------------------------------------------------- staging ---


def test_staging_copies_files_and_leaves_the_original_alone(repo: Path, tmp_path: Path) -> None:
    staged = stage_repository(repo, tmp_path / "copy", max_bytes=1_000_000)
    assert (staged.root / "src" / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    (staged.root / "src" / "app.py").write_text("mutated\n", encoding="utf-8")
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "value = 1\n"


def test_staging_skips_build_and_vcs_trees(repo: Path, tmp_path: Path) -> None:
    for name in (".git", ".venv", "node_modules", "__pycache__"):
        (repo / name).mkdir()
        (repo / name / "junk.txt").write_text("x" * 100, encoding="utf-8")
    staged = stage_repository(repo, tmp_path / "copy", max_bytes=1_000_000)
    assert staged.files == 1
    assert not (staged.root / ".git").exists()
    assert all(name in STAGING_EXCLUDED for name in (".git", ".venv", "node_modules"))


def test_staging_refuses_symlinks(repo: Path, tmp_path: Path) -> None:
    """A link into the host filesystem would be a way out of the bind mount."""
    (repo / "escape").symlink_to("/etc/passwd")
    staged = stage_repository(repo, tmp_path / "copy", max_bytes=1_000_000)
    assert not (staged.root / "escape").exists()
    assert any("symlink" in entry for entry in staged.excluded)


def test_staging_enforces_a_size_ceiling(repo: Path, tmp_path: Path) -> None:
    (repo / "big.bin").write_bytes(b"0" * 5000)
    with pytest.raises(SandboxError, match="too large"):
        stage_repository(repo, tmp_path / "copy", max_bytes=1000)


def test_staging_a_missing_repository_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="does not exist"):
        stage_repository(tmp_path / "absent", tmp_path / "copy", max_bytes=1000)


def test_staging_into_the_repository_is_refused(repo: Path) -> None:
    """Otherwise the walk copies its own output until the filesystem stops it."""
    with pytest.raises(SandboxError, match="inside the repository"):
        stage_repository(repo, repo / "copy", max_bytes=1_000_000)


def test_staging_an_unreadable_directory_is_reported(repo: Path, tmp_path: Path) -> None:
    locked = repo / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        with pytest.raises(SandboxError, match="could not read directory"):
            stage_repository(repo, tmp_path / "copy", max_bytes=1_000_000)
    finally:
        locked.chmod(0o755)


def test_staging_reports_irregular_entries(repo: Path, tmp_path: Path) -> None:
    import os

    os.mkfifo(repo / "pipe")
    staged = stage_repository(repo, tmp_path / "copy", max_bytes=1_000_000)
    assert any("not a regular file" in entry for entry in staged.excluded)


def test_staging_preserves_nested_directories(repo: Path, tmp_path: Path) -> None:
    nested = repo / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "deep.py").write_text("x = 1\n", encoding="utf-8")
    staged = stage_repository(repo, tmp_path / "copy", max_bytes=1_000_000)
    assert (staged.root / "a" / "b" / "c" / "deep.py").is_file()


# ------------------------------------------------------------------ models ---


def test_only_a_completed_command_counts_as_evidence() -> None:
    assert CheckStatus.PASSED.is_evidence
    assert CheckStatus.FAILED.is_evidence
    assert not CheckStatus.SKIPPED.is_evidence
    assert not CheckStatus.UNAVAILABLE.is_evidence
    assert not CheckStatus.TIMED_OUT.is_evidence


def test_only_the_verified_verdict_is_verified() -> None:
    assert Verdict.VERIFIED.is_verified
    for verdict in (Verdict.REGRESSED, Verdict.INCONCLUSIVE, Verdict.ERRORED):
        assert not verdict.is_verified


def test_checks_are_ordered_by_what_they_prove() -> None:
    assert CheckKind.TESTS.strength < CheckKind.LINT.strength < CheckKind.SYNTAX.strength


def test_output_is_truncated_from_the_middle() -> None:
    """The first error and the final summary both matter; the middle rarely does."""
    text = "HEAD" + ("x" * 1000) + "TAIL"
    shortened, truncated = truncate_output(text, limit=100)
    assert truncated
    assert shortened.startswith("HEAD")
    assert shortened.endswith("TAIL")
    assert "elided" in shortened


def test_short_output_is_left_alone() -> None:
    assert truncate_output("fine", limit=100) == ("fine", False)


def test_a_command_outcome_joins_both_streams() -> None:
    outcome = CommandOutcome(command=("x",), exit_code=1, stdout="out", stderr="err")
    assert outcome.output == "out\nerr"
    assert not outcome.succeeded
    assert outcome.tail(1) == "err"


def test_a_skipped_check_carries_no_command() -> None:
    result = CheckResult.skipped(CheckKind.LINT, "ruff", "not configured")
    assert result.outcome is None
    assert not result.passed


def test_a_file_that_cannot_be_copied_is_reported(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("input/output error")

    monkeypatch.setattr(shutil, "copy2", explode)
    with pytest.raises(SandboxError, match="could not copy file"):
        stage_repository(repo, tmp_path / "copy", max_bytes=1_000_000)
