"""Tests for `run_migration`, and above all for when it refuses to write.

Everything before Phase 7 was safe by construction — no command could modify a
repository. This one can, so the tests that matter most are the ones proving it
does not, and that the reasons it gives are true.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from rewire.core.config import Settings
from rewire.gitio import inspect_working_tree
from rewire.llm import ScriptBuilder, ScriptedProvider
from rewire.sandbox.models import (
    CheckKind,
    CheckResult,
    CheckStatus,
    CommandOutcome,
    Verdict,
    VerificationReport,
)
from rewire.services import MigrationRequest, MigrationStatus, run_migration

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


def git(root: Path, *args: str) -> None:
    """Run git in a throwaway repository, isolated from the developer's config.

    ``core.hooksPath`` is neutralised because a global commit-msg hook — a
    conventional-commit linter, say — would otherwise fail these fixtures on one
    machine and pass on another.
    """
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


@pytest.fixture
def case(tmp_path: Path) -> Path:
    """A git repository with a breaking change waiting to be migrated."""
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
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "chore: initial")
    return tmp_path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / ".rewire")


def provider_editing(new_text: str = "max_completion_tokens=100") -> ScriptedProvider:
    return (
        ScriptBuilder()
        .calls(
            "propose_edit",
            file="app.py",
            old_text="max_tokens=100",
            new_text=new_text,
            rationale="renamed",
        )
        .says("Renamed the request field.")
        .build()
    )


def verifier_saying(verdict: Verdict) -> Callable[..., VerificationReport]:
    tests = CheckResult(
        kind=CheckKind.TESTS,
        name="pytest",
        status=CheckStatus.PASSED if verdict is Verdict.VERIFIED else CheckStatus.FAILED,
        outcome=CommandOutcome(command=("pytest",), exit_code=0),
        reason="",
    )
    report = VerificationReport(
        verdict=verdict, reason=f"scripted {verdict.value}", baseline=(tests,), patched=(tests,)
    )
    return lambda *_a, **_k: report


def migrate(
    case: Path, settings: Settings, provider: ScriptedProvider, verdict: Verdict, **kwargs: object
):
    return run_migration(
        MigrationRequest(
            repository=case / "repo",
            old_spec=case / "old.yaml",
            new_spec=case / "new.yaml",
            **kwargs,  # type: ignore[arg-type]
        ),
        provider=provider,
        settings=settings,
        verifier=verifier_saying(verdict),
    )


# ---------------------------------------------------------------- outcomes ---


def test_a_verified_patch_is_reported_but_not_written_by_default(
    case: Path, settings: Settings
) -> None:
    """Reporting is the default; writing has to be asked for."""
    outcome = migrate(case, settings, provider_editing(), Verdict.VERIFIED)
    assert outcome.status is MigrationStatus.VERIFIED
    assert outcome.verified
    assert outcome.written == ()
    assert (case / "repo" / "app.py").read_text(encoding="utf-8") == CLIENT


def test_a_verified_patch_is_written_when_asked(case: Path, settings: Settings) -> None:
    outcome = migrate(case, settings, provider_editing(), Verdict.VERIFIED, apply=True)
    assert outcome.status is MigrationStatus.APPLIED
    assert outcome.written == ("app.py",)
    assert "max_completion_tokens=100" in (case / "repo" / "app.py").read_text(encoding="utf-8")


def test_the_written_change_is_visible_to_git(case: Path, settings: Settings) -> None:
    """The point of refusing a dirty tree: `git diff` must show exactly this."""
    migrate(case, settings, provider_editing(), Verdict.VERIFIED, apply=True)
    tree = inspect_working_tree(case / "repo")
    assert tree.dirty == ("app.py",)


def test_no_breaking_changes_is_a_success_not_a_failure(case: Path, settings: Settings) -> None:
    """Most runs will say this once specs are watched automatically."""
    (case / "new.yaml").write_text(SPEC.format(v="2", field="max_tokens"), encoding="utf-8")
    outcome = migrate(case, settings, provider_editing(), Verdict.VERIFIED, apply=True)
    assert outcome.status is MigrationStatus.NO_BREAKING_CHANGES
    assert outcome.status.is_success
    assert outcome.written == ()


def test_an_unaffected_repository_is_a_success(case: Path, settings: Settings) -> None:
    (case / "repo" / "app.py").write_text("value = 1\n", encoding="utf-8")
    git(case / "repo", "commit", "-aqm", "chore: unrelated")
    outcome = migrate(case, settings, provider_editing(), Verdict.VERIFIED)
    assert outcome.status is MigrationStatus.NO_AFFECTED_CODE
    assert outcome.status.is_success


def test_an_agent_that_proposes_nothing_is_a_failure(case: Path, settings: Settings) -> None:
    provider = ScriptBuilder().says("Nothing to do.").says("Still nothing.").build()
    outcome = migrate(case, settings, provider, Verdict.VERIFIED)
    assert outcome.status is MigrationStatus.NO_PATCH
    assert not outcome.status.is_success


# ---------------------------------------------------------------- refusals ---


def test_an_unverified_patch_is_never_written(case: Path, settings: Settings) -> None:
    """There is deliberately no flag for this. The sandbox is the whole point."""
    outcome = migrate(case, settings, provider_editing(), Verdict.REGRESSED, apply=True)
    assert outcome.status is MigrationStatus.UNVERIFIED
    assert outcome.written == ()
    assert (case / "repo" / "app.py").read_text(encoding="utf-8") == CLIENT


def test_writing_into_a_dirty_tree_is_refused(case: Path, settings: Settings) -> None:
    """Otherwise Rewire's change and the user's become one undoable diff."""
    (case / "repo" / "notes.txt").write_text("work in progress\n", encoding="utf-8")
    outcome = migrate(case, settings, provider_editing(), Verdict.VERIFIED, apply=True)
    assert outcome.status is MigrationStatus.REFUSED
    assert "uncommitted changes" in outcome.refusal
    assert "notes.txt" in outcome.refusal
    assert (case / "repo" / "app.py").read_text(encoding="utf-8") == CLIENT


def test_a_dirty_tree_can_be_overridden_explicitly(case: Path, settings: Settings) -> None:
    (case / "repo" / "notes.txt").write_text("work in progress\n", encoding="utf-8")
    outcome = migrate(
        case, settings, provider_editing(), Verdict.VERIFIED, apply=True, allow_dirty=True
    )
    assert outcome.status is MigrationStatus.APPLIED


def test_writing_outside_a_git_repository_is_refused(tmp_path: Path, settings: Settings) -> None:
    """Without Git there is no review and no undo, so there is no safety net."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "pyproject.toml").write_text(
        '[project]\nname="a"\nversion="1"\ndependencies=["openai"]\n', encoding="utf-8"
    )
    (plain / "app.py").write_text(CLIENT, encoding="utf-8")
    (tmp_path / "old.yaml").write_text(SPEC.format(v="1", field="max_tokens"), encoding="utf-8")
    (tmp_path / "new.yaml").write_text(
        SPEC.format(v="2", field="max_completion_tokens"), encoding="utf-8"
    )
    outcome = run_migration(
        MigrationRequest(
            repository=plain,
            old_spec=tmp_path / "old.yaml",
            new_spec=tmp_path / "new.yaml",
            apply=True,
        ),
        provider=provider_editing(),
        settings=settings,
        verifier=verifier_saying(Verdict.VERIFIED),
    )
    assert outcome.status is MigrationStatus.REFUSED
    assert "not a Git repository" in outcome.refusal


def test_a_repository_that_moved_after_verification_is_refused(
    case: Path, settings: Settings
) -> None:
    """The window between verifying and writing is where a tree can change."""
    verified = verifier_saying(Verdict.VERIFIED)

    def verify_then_move(*args: object, **kwargs: object) -> VerificationReport:
        report = verified(*args, **kwargs)
        (case / "repo" / "app.py").write_text("something else entirely\n", encoding="utf-8")
        return report

    outcome = run_migration(
        MigrationRequest(
            repository=case / "repo",
            old_spec=case / "old.yaml",
            new_spec=case / "new.yaml",
            apply=True,
            allow_dirty=True,
        ),
        provider=provider_editing(),
        settings=settings,
        verifier=verify_then_move,
    )
    assert outcome.status is MigrationStatus.REFUSED
    assert "changed since the patch was proposed" in outcome.refusal
    assert (case / "repo" / "app.py").read_text(encoding="utf-8") == "something else entirely\n"


def test_a_dirty_tree_is_refused_before_any_model_is_called(case: Path, settings: Settings) -> None:
    """The answer is available in milliseconds; paying for an agent run first is waste."""
    (case / "repo" / "notes.txt").write_text("work in progress\n", encoding="utf-8")
    provider = provider_editing()
    outcome = migrate(case, settings, provider, Verdict.VERIFIED, apply=True)
    assert outcome.status is MigrationStatus.REFUSED
    assert outcome.repair is None
    assert provider.requests == []


# ------------------------------------------------------------------ record ---


def test_every_run_writes_a_machine_readable_record(case: Path, settings: Settings) -> None:
    """Phase 8 aggregates these; a dataset of only interesting runs has a hole."""
    outcome = migrate(case, settings, provider_editing(), Verdict.VERIFIED, apply=True)
    record = json.loads(
        (settings.runs_dir / outcome.run_id / "migration.json").read_text(encoding="utf-8")
    )
    assert record["status"] == "applied"
    assert record["files_written"] == ["app.py"]
    assert record["attempts"][0]["verdict"] == "verified"
    assert record["affected_locations"] > 0


def test_a_run_that_did_nothing_is_still_recorded(case: Path, settings: Settings) -> None:
    (case / "new.yaml").write_text(SPEC.format(v="2", field="max_tokens"), encoding="utf-8")
    outcome = migrate(case, settings, provider_editing(), Verdict.VERIFIED)
    record = json.loads(
        (settings.runs_dir / outcome.run_id / "migration.json").read_text(encoding="utf-8")
    )
    assert record["status"] == "no_breaking_changes"
    assert record["attempts"] == []


def test_every_status_has_a_summary_sentence() -> None:
    """A status with no sentence would render as an empty line in the report."""
    from rewire.services.migrate import MigrationOutcome

    for status in MigrationStatus:
        outcome = MigrationOutcome(run_id="r", status=status)
        assert outcome.summary_line()
