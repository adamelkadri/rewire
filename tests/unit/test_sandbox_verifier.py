"""Tests for the verification pipeline: baseline, apply, re-run, compare.

These run against a scripted runner rather than Docker. The logic being tested
— what counts as a regression, what counts as evidence, what the network policy
is — is independent of the backend, and testing it through real containers
would make it slow enough to be skipped, which is how verification logic ends
up unverified. The container itself is covered by the integration tests in
``test_sandbox_docker.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rewire.agents.patch import CandidatePatch, FileChange, FileEdit, PatchBuilder
from rewire.core.errors import PatchError, SandboxError
from rewire.sandbox.models import (
    CheckKind,
    CheckResult,
    CheckStatus,
    CommandOutcome,
    Verdict,
    VerificationRequest,
)
from rewire.sandbox.scripted import ScriptedRunner
from rewire.sandbox.toolchain import Check
from rewire.sandbox.verifier import _write_overlay, classify, decide, verify

SOURCE = 'def payload():\n    return {"max_tokens": 1}\n'
TEST = 'from app import payload\n\n\ndef test_field():\n    assert "max_tokens" in payload()\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with a test suite, a linter and a type checker configured."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    (root / "tests" / "test_app.py").write_text(TEST, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1"\n[tool.ruff]\n[tool.mypy]\n', encoding="utf-8"
    )
    return root


@pytest.fixture
def runner() -> ScriptedRunner:
    return ScriptedRunner()


@pytest.fixture
def factory(runner: ScriptedRunner):
    def build(root: Path, request: VerificationRequest) -> ScriptedRunner:
        build.root = root  # type: ignore[attr-defined]
        return runner

    return build


def make_patch(repo: Path, file: str, old: str, new: str) -> CandidatePatch:
    """Build a patch against the repository's real content."""
    builder = PatchBuilder(read_file=lambda path: (repo / path).read_text(encoding="utf-8"))
    builder.add(FileEdit(file=file, old_text=old, new_text=new))
    return builder.build("rename the field")


# ---------------------------------------------------------------- verdicts ---


def test_a_passing_patch_is_verified(repo: Path, runner: ScriptedRunner, factory) -> None:
    patch = make_patch(repo, "app.py", "max_tokens", "max_completion_tokens")
    report = verify(repo, patch, runner_factory=factory)
    assert report.verdict is Verdict.VERIFIED
    assert report.verified
    assert report.files_changed == ("app.py",)


def test_breaking_a_previously_passing_check_is_a_regression(
    repo: Path, runner: ScriptedRunner, factory
) -> None:
    """The patched run fails a check the baseline passed. That is the agent's doing."""
    # Passes once — the baseline — then fails, which is the shape of a patch
    # that changed a call site without changing the test that asserts on it.
    runner.when("-m pytest", exit_code=0, times=1)
    runner.when("-m pytest", exit_code=1, stdout="1 failed")
    report = verify(repo, make_patch(repo, "app.py", "max_tokens", "nope"), runner_factory=factory)
    assert report.verdict is Verdict.REGRESSED
    assert report.regressions == (CheckKind.TESTS,)
    assert "tests" in report.reason


def test_a_check_that_was_already_failing_is_not_blamed_on_the_patch(
    repo: Path, runner: ScriptedRunner, factory
) -> None:
    """Without a baseline this repository would make every patch look broken."""
    runner.when("-m mypy", exit_code=1, stdout="error: bad")
    report = verify(repo, make_patch(repo, "app.py", "max_tokens", "x"), runner_factory=factory)
    assert report.verdict is Verdict.VERIFIED
    assert report.regressions == ()
    assert report.pre_existing_failures == (CheckKind.TYPECHECK,)


def test_a_patch_that_fixes_a_failing_suite_is_verified(
    repo: Path, runner: ScriptedRunner, factory
) -> None:
    runner.when("-m pytest", exit_code=1, stdout="1 failed", times=1)
    report = verify(repo, make_patch(repo, "app.py", "max_tokens", "x"), runner_factory=factory)
    assert report.verdict is Verdict.VERIFIED


def test_a_repository_without_tests_can_never_be_verified(
    tmp_path: Path, runner: ScriptedRunner, factory
) -> None:
    """Nothing exercised the code, so a green run proves only that it parses."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    report = verify(root, make_patch(root, "app.py", "max_tokens", "x"), runner_factory=factory)
    assert report.verdict is Verdict.INCONCLUSIVE
    assert "no test suite" in report.reason


def test_an_unrunnable_test_suite_is_not_a_pass(
    repo: Path, runner: ScriptedRunner, factory
) -> None:
    runner.when("-m pytest", exit_code=127, stderr="python: No module named pytest")
    report = verify(repo, make_patch(repo, "app.py", "max_tokens", "x"), runner_factory=factory)
    assert report.verdict is Verdict.INCONCLUSIVE
    assert "never run" in report.reason
    assert report.check(CheckKind.TESTS).status is CheckStatus.UNAVAILABLE


def test_a_timed_out_suite_is_not_a_pass(repo: Path, runner: ScriptedRunner, factory) -> None:
    runner.when("-m pytest", timed_out=True)
    report = verify(repo, make_patch(repo, "app.py", "max_tokens", "x"), runner_factory=factory)
    assert report.verdict is Verdict.INCONCLUSIVE
    assert "time budget" in report.reason


def test_collecting_no_tests_is_not_a_pass(repo: Path, runner: ScriptedRunner, factory) -> None:
    """Pytest exits 5 for an empty run; that is an absence of evidence, not a pass."""
    runner.when("-m pytest", exit_code=5, stdout="no tests ran")
    report = verify(repo, make_patch(repo, "app.py", "max_tokens", "x"), runner_factory=factory)
    assert report.verdict is Verdict.INCONCLUSIVE
    assert "collected no tests" in report.reason


def test_a_suite_failing_before_and_after_confirms_nothing(
    repo: Path, runner: ScriptedRunner, factory
) -> None:
    runner.when("-m pytest", exit_code=1, stdout="1 failed")
    report = verify(repo, make_patch(repo, "app.py", "max_tokens", "x"), runner_factory=factory)
    assert report.verdict is Verdict.INCONCLUSIVE
    assert "already failing" in report.reason
    assert report.regressions == ()


# ------------------------------------------------------------------ inputs ---


def test_a_baseline_run_needs_no_patch(repo: Path, runner: ScriptedRunner, factory) -> None:
    report = verify(repo, runner_factory=factory)
    assert report.verdict is Verdict.INCONCLUSIVE
    assert "no patch was supplied" in report.reason
    assert report.baseline and not report.patched


def test_an_empty_patch_is_not_verified(repo: Path, runner: ScriptedRunner, factory) -> None:
    report = verify(repo, CandidatePatch(), runner_factory=factory)
    assert report.verdict is Verdict.INCONCLUSIVE
    assert "changes nothing" in report.reason


def test_a_patch_whose_assumptions_no_longer_hold_is_refused(
    repo: Path, runner: ScriptedRunner, factory
) -> None:
    """Writing ``after`` over a file that has since changed would discard the change."""
    stale = CandidatePatch(
        changes=(FileChange(file="app.py", before="something else entirely\n", after="new\n"),)
    )
    with pytest.raises(PatchError, match="changed since the patch was proposed"):
        verify(repo, stale, runner_factory=factory)


def test_a_patch_that_cannot_be_written_errors(
    repo: Path, runner: ScriptedRunner, factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> list[str]:
        raise PatchError("disk is full", file="app.py")

    monkeypatch.setattr("rewire.sandbox.verifier.write_patch", explode)
    report = verify(repo, make_patch(repo, "app.py", "max_tokens", "x"), runner_factory=factory)
    assert report.verdict is Verdict.ERRORED
    assert "could not be applied" in report.reason


def test_a_patch_creating_a_new_file_is_allowed(
    repo: Path, runner: ScriptedRunner, factory
) -> None:
    patch = CandidatePatch(changes=(FileChange(file="new.py", before="", after="x = 1\n"),))
    report = verify(repo, patch, runner_factory=factory)
    assert report.verdict is Verdict.VERIFIED
    assert report.files_changed == ("new.py",)


def test_the_original_repository_is_never_touched(
    repo: Path, runner: ScriptedRunner, factory
) -> None:
    """Everything happens in a copy; the user's checkout is read-only throughout."""
    patch = make_patch(repo, "app.py", "max_tokens", "max_completion_tokens")
    verify(repo, patch, runner_factory=factory)
    assert (repo / "app.py").read_text(encoding="utf-8") == SOURCE


# ----------------------------------------------------------------- policy ---


def test_checks_never_have_network_access(repo: Path, runner: ScriptedRunner, factory) -> None:
    verify(repo, make_patch(repo, "app.py", "max_tokens", "x"), runner_factory=factory)
    checks = [call for call in runner.calls if call.command[0] not in {"pip", "python3"}]
    compile_steps = [call for call in runner.calls if "compileall" in call.command]
    assert checks and compile_steps
    assert {call.network for call in (*checks, *compile_steps)} == {"none"}


def test_only_installation_reaches_the_network(repo: Path, runner: ScriptedRunner, factory) -> None:
    """Installing runs the repository's build backend, so it is confined but online."""
    verify(repo, make_patch(repo, "app.py", "max_tokens", "x"), runner_factory=factory)
    online = [call.command for call in runner.calls if call.network == "bridge"]
    assert all(command[0] in {"pip", "python3"} for command in online)
    assert any("install" in command for command in online)


def test_installation_can_be_disabled_for_a_fully_offline_run(
    repo: Path, runner: ScriptedRunner, factory
) -> None:
    verify(
        repo,
        make_patch(repo, "app.py", "max_tokens", "x"),
        request=VerificationRequest(install=False),
        runner_factory=factory,
    )
    assert all(call.network == "none" for call in runner.calls)


def test_a_repository_with_no_dependencies_stays_offline(
    tmp_path: Path, runner: ScriptedRunner, factory
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    verify(root, runner_factory=factory)
    assert all(call.network == "none" for call in runner.calls)


def test_a_failed_installation_is_reported_rather_than_raised(
    repo: Path, runner: ScriptedRunner, factory
) -> None:
    """The checks that need no dependencies still produce evidence."""
    runner.when("pip install", exit_code=1, stderr="ERROR: could not build wheel")
    runner.when("-m pytest", exit_code=127, stderr="No module named pytest")
    report = verify(repo, make_patch(repo, "app.py", "max_tokens", "x"), runner_factory=factory)
    assert report.install is not None
    assert report.install.status is CheckStatus.FAILED
    assert report.verdict is Verdict.INCONCLUSIVE
    assert "dependency installation failed" in report.reason
    assert report.check(CheckKind.SYNTAX).passed


def test_checks_are_given_the_configured_timeout(
    repo: Path, runner: ScriptedRunner, factory
) -> None:
    verify(repo, request=VerificationRequest(check_timeout_seconds=42), runner_factory=factory)
    assert {call.timeout for call in runner.calls if call.network == "none"} == {42}


def test_skipped_checks_are_carried_into_the_report(
    tmp_path: Path, runner: ScriptedRunner, factory
) -> None:
    """A report that silently omits what was not run reads as fuller than it is."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    report = verify(root, runner_factory=factory)
    assert report.check(CheckKind.TESTS, patched=False).status is CheckStatus.SKIPPED


# --------------------------------------------------------------- unit logic ---

CHECK = Check(kind=CheckKind.TESTS, name="pytest", command=("pytest",), reason="has tests")


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (CommandOutcome(command=("pytest",), exit_code=0), CheckStatus.PASSED),
        (CommandOutcome(command=("pytest",), exit_code=1), CheckStatus.FAILED),
        (CommandOutcome(command=("pytest",), exit_code=127), CheckStatus.UNAVAILABLE),
        (
            CommandOutcome(command=("pytest",), exit_code=None, timed_out=True),
            CheckStatus.TIMED_OUT,
        ),
        (CommandOutcome(command=("pytest",), exit_code=5), CheckStatus.SKIPPED),
        (
            CommandOutcome(command=("pytest",), exit_code=1, stderr="No module named pytest"),
            CheckStatus.UNAVAILABLE,
        ),
    ],
)
def test_classification_of_outcomes(outcome: CommandOutcome, expected: CheckStatus) -> None:
    assert classify(CHECK, outcome)[0] is expected


def test_exit_five_is_only_special_for_pytest() -> None:
    """Another tool's exit 5 is a genuine failure, not an empty collection."""
    ruff = Check(kind=CheckKind.LINT, name="ruff", command=("ruff",), reason="")
    assert classify(ruff, CommandOutcome(command=("ruff",), exit_code=5))[0] is CheckStatus.FAILED


def test_an_unavailable_tool_cannot_regress() -> None:
    """A tool missing in both runs measured nothing, so it cannot have broken."""
    missing = CheckResult(
        kind=CheckKind.LINT, name="ruff", status=CheckStatus.UNAVAILABLE, reason=""
    )
    verdict, _, regressions, _ = decide([missing], [missing], install=None)
    assert regressions == ()
    assert verdict is Verdict.INCONCLUSIVE


def test_a_check_appearing_only_after_the_patch_cannot_regress() -> None:
    passing = CheckResult(kind=CheckKind.TESTS, name="pytest", status=CheckStatus.PASSED, reason="")
    verdict, _, regressions, _ = decide([], [passing], install=None)
    assert regressions == ()
    assert verdict is Verdict.VERIFIED


def test_unchanged_files_in_a_patch_are_ignored(
    repo: Path, runner: ScriptedRunner, factory
) -> None:
    """A patch may carry a file it inspected and decided not to touch."""
    patch = CandidatePatch(
        changes=(
            FileChange(file="app.py", before=SOURCE, after=SOURCE),
            FileChange(file="tests/test_app.py", before=TEST, after=TEST.replace("max_", "MAX_")),
        )
    )
    report = verify(repo, patch, runner_factory=factory)
    assert report.files_changed == ("tests/test_app.py",)
    assert report.verdict is Verdict.VERIFIED


def test_the_scripted_runner_reports_the_commands_it_saw(
    repo: Path, runner: ScriptedRunner, factory
) -> None:
    verify(repo, runner_factory=factory)
    assert any("compileall" in line for line in runner.commands())


def test_a_rule_stops_matching_once_its_uses_are_spent() -> None:
    scripted = ScriptedRunner().when("-m pytest", exit_code=3, times=1)
    first = scripted.run(("python", "-m", "pytest"), timeout=1)
    second = scripted.run(("python", "-m", "pytest"), timeout=1)
    assert (first.exit_code, second.exit_code) == (3, 0)


# ----------------------------------------------------------------- overlay ---


def test_an_overlay_is_written_into_the_staged_copy(tmp_path: Path) -> None:
    """The hidden contract test reaches the sandbox this way and no other."""
    root = tmp_path / "staged"
    root.mkdir()
    written = _write_overlay(
        root, {"tests/test_contract.py": "def test_x():\n    pass\n", "note.txt": "hello"}
    )
    assert written == ("note.txt", "tests/test_contract.py")
    assert (root / "tests/test_contract.py").read_text(encoding="utf-8").startswith("def test_x")


def test_an_overlay_creates_the_directories_it_needs(tmp_path: Path) -> None:
    root = tmp_path / "staged"
    root.mkdir()
    _write_overlay(root, {"a/b/c/test_deep.py": "x = 1\n"})
    assert (root / "a/b/c/test_deep.py").is_file()


def test_a_symlinked_root_is_not_mistaken_for_an_escape(tmp_path: Path) -> None:
    """A symlinked root must not be mistaken for an escape.

    A macOS temporary directory is itself a symlink, and resolving only one side
    made every legitimate overlay path look like one.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert _write_overlay(link, {"tests/test_x.py": "x = 1\n"}) == ("tests/test_x.py",)
    assert (real / "tests/test_x.py").is_file()


@pytest.mark.parametrize("escape", ["../outside.py", "tests/../../outside.py"])
def test_an_overlay_path_may_not_escape_the_staged_copy(tmp_path: Path, escape: str) -> None:
    """An overlay path may not write outside the staged copy.

    The overlay is dataset-authored, but a path escaping the copy would reach the
    host, so the guard is not optional.
    """
    root = tmp_path / "staged"
    root.mkdir()
    with pytest.raises(SandboxError, match="escapes the staged repository"):
        _write_overlay(root, {escape: "danger = 1\n"})
    assert not (tmp_path / "outside.py").exists()


def test_an_absolute_overlay_path_escapes_too(tmp_path: Path) -> None:
    root = tmp_path / "staged"
    root.mkdir()
    with pytest.raises(SandboxError, match="escapes the staged repository"):
        _write_overlay(root, {str(tmp_path / "elsewhere.py"): "danger = 1\n"})


def test_an_unwritable_overlay_is_a_sandbox_error(tmp_path: Path) -> None:
    root = tmp_path / "staged"
    root.mkdir()
    (root / "tests").write_text("this is a file, not a directory", encoding="utf-8")
    with pytest.raises(SandboxError, match="could not write overlay file"):
        _write_overlay(root, {"tests/test_x.py": "x = 1\n"})


def test_an_overlay_adds_a_test_suite_to_a_repository_that_had_none(
    tmp_path: Path, runner: ScriptedRunner, factory
) -> None:
    """A repository with no tests of its own gains one from the overlay.

    The check plan therefore has to be recomputed after the overlay is written,
    rather than reused from the baseline run.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname="a"\nversion="1"\n', encoding="utf-8")

    report = verify(
        root,
        make_patch(root, "app.py", "max_tokens", "max_completion_tokens"),
        runner_factory=factory,
        overlay={"tests/test_contract.py": TEST},
    )
    assert any(check.kind is CheckKind.TESTS for check in report.patched)
