"""Tests for publishing a verified patch as a pull request.

Most of these are refusals. Publishing is the first thing in Rewire that reaches
outside the machine, so what it *declines* to do carries more weight than what it
does: an unverified patch never leaves, a dirty tree is never committed, and a
capability to merge does not exist anywhere for a future flag to reach.

``gh`` is stubbed. Git is real, because the guarantees being tested are about the
state a repository ends up in.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from rewire.agents.migration_agent import MigrationResult
from rewire.agents.patch import CandidatePatch, FileEdit, PatchBuilder
from rewire.agents.trace import RunSummary
from rewire.changes.differ import diff_specs
from rewire.changes.spec import parse_spec_text
from rewire.gitio import branch as git
from rewire.gitio import github
from rewire.sandbox.models import (
    CheckKind,
    CheckResult,
    CheckStatus,
    CommandOutcome,
    Verdict,
    VerificationReport,
)
from rewire.services.migrate import MigrationOutcome, MigrationStatus
from rewire.services.publish import (
    PublishRequest,
    PublishStatus,
    build_body,
    build_title,
    check_publishable,
    publish,
)
from rewire.services.repair import Attempt, RepairOutcome

SPEC = """openapi: "3.0.3"
info: {{title: Example API, version: "{v}"}}
paths:
  /v1/chat:
    post:
      requestBody:
        content:
          application/json:
            schema: {{type: object, properties: {{{field}: {{type: integer}}}}}}
      responses: {{'200': {{description: OK}}}}
"""


def run(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args],  # noqa: S607 - git is on PATH in CI and locally
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    run(root, "init", "--initial-branch=main")
    run(root, "config", "user.email", "test@example.com")
    run(root, "config", "user.name", "Test")
    run(root, "config", "core.hooksPath", "/dev/null")
    (root / "app.py").write_text('PAYLOAD = {"max_tokens": 5}\n', encoding="utf-8")
    (root / "notes.txt").write_text("mine\n", encoding="utf-8")
    run(root, "add", "-A")
    run(root, "commit", "-m", "initial")
    run(root, "remote", "add", "origin", "https://example.invalid/a.git")
    return root


@pytest.fixture(autouse=True)
def stub_github(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Replace every GitHub call. Nothing in this suite reaches the network."""
    opened: list[dict[str, object]] = []

    def fake_open(root: Path, **kwargs: object) -> github.PullRequest:
        opened.append(kwargs)
        return github.PullRequest(url="https://github.com/o/r/pull/7", number=7)

    monkeypatch.setattr(github, "is_authenticated", lambda _root: True)
    monkeypatch.setattr(
        github,
        "describe_repository",
        lambda _root: github.Repository(owner="o", name="r", default_branch="main"),
    )
    monkeypatch.setattr(github, "open_pull_request", fake_open)
    monkeypatch.setattr(git, "push", lambda *_a, **_k: None)
    return opened


def patch_for(repo: Path) -> CandidatePatch:
    builder = PatchBuilder(read_file=lambda path: (repo / path).read_text(encoding="utf-8"))
    builder.add(FileEdit(file="app.py", old_text="max_tokens", new_text="max_completion_tokens"))
    return builder.build("renamed the field")


def report(verdict: Verdict = Verdict.VERIFIED) -> VerificationReport:
    passed = verdict is Verdict.VERIFIED
    check = CheckResult(
        kind=CheckKind.TESTS,
        name="pytest",
        status=CheckStatus.PASSED if passed else CheckStatus.FAILED,
        outcome=CommandOutcome(command=("pytest",), exit_code=0 if passed else 1),
        reason="",
    )
    return VerificationReport(
        verdict=verdict,
        reason="the test suite passed after the patch and no check regressed",
        baseline=(check,),
        patched=(check,),
        image="python:3.12-slim",
    )


def outcome_for(
    repo: Path,
    *,
    status: MigrationStatus = MigrationStatus.VERIFIED,
    patch: CandidatePatch | None = None,
    verdict: Verdict = Verdict.VERIFIED,
) -> MigrationOutcome:
    changes = diff_specs(
        parse_spec_text(SPEC.format(v="1", field="max_tokens"), source="old.yaml"),
        parse_spec_text(SPEC.format(v="2", field="max_completion_tokens"), source="new.yaml"),
    )
    candidate = patch_for(repo) if patch is None else patch
    attempt = Attempt(
        number=1,
        result=MigrationResult(
            summary=RunSummary(
                run_id="r",
                repository="repo",
                cost_usd=0.0123,
                outcome="proposed a patch",
            ),
            patch=candidate,
            trace=None,  # type: ignore[arg-type]
            final_message="Renamed the request field.",
        ),
        report=report(verdict),
    )
    return MigrationOutcome(
        run_id="abc123",
        status=status,
        changes=changes,
        repair=RepairOutcome(attempts=(attempt,), stopped_because="verified"),
    )


# ------------------------------------------------------------- refusals ---


def test_an_unverified_patch_is_never_published(repo: Path) -> None:
    """An unverified patch never leaves the machine.

    The measured overclaim rate is an argument for a human reviewer, not an
    argument for publishing weaker evidence and hoping they catch it.
    """
    result = publish(
        outcome_for(repo, status=MigrationStatus.UNVERIFIED, verdict=Verdict.REGRESSED),
        PublishRequest(repository=repo),
    )
    assert result.status is PublishStatus.REFUSED
    assert "only publishes a patch the sandbox verified" in result.refusal
    assert "no override" in result.refusal
    assert run(repo, "branch", "--list") == "* main"


def test_a_weakened_patch_is_never_published(repo: Path) -> None:
    """A patch that passed by removing checks is exactly what must not be proposed."""
    result = publish(
        outcome_for(repo, status=MigrationStatus.UNVERIFIED, verdict=Verdict.WEAKENED),
        PublishRequest(repository=repo),
    )
    assert result.status is PublishStatus.REFUSED


def test_an_empty_patch_is_nothing_to_publish_not_a_failure(repo: Path) -> None:
    result = publish(
        outcome_for(repo, status=MigrationStatus.NO_AFFECTED_CODE, patch=CandidatePatch()),
        PublishRequest(repository=repo),
    )
    assert result.status is PublishStatus.NOTHING_TO_PUBLISH


def test_a_dirty_tree_is_refused(repo: Path) -> None:
    """A pull request must contain Rewire's change and nothing else."""
    (repo / "notes.txt").write_text("my unrelated edit\n", encoding="utf-8")
    assert "uncommitted changes" in check_publishable(repo)
    result = publish(outcome_for(repo), PublishRequest(repository=repo))
    assert result.status is PublishStatus.REFUSED


def test_an_untracked_file_also_counts_as_dirty(repo: Path) -> None:
    """Deliberately strict, and the same rule --apply uses.

    An untracked file cannot be committed by accident here, because only the
    patch's own paths are staged. Refusing anyway keeps one definition of "safe
    to write into" across the whole tool, and gitignored files do not count, so
    in practice this fires on something the user genuinely ought to look at.
    """
    (repo / "untracked.py").write_text("not mine\n", encoding="utf-8")
    assert "untracked.py" in check_publishable(repo)


def test_a_directory_that_is_not_a_repository_is_refused(tmp_path: Path) -> None:
    assert "not a Git repository" in check_publishable(tmp_path)


def test_a_repository_with_no_remote_is_refused(repo: Path) -> None:
    run(repo, "remote", "remove", "origin")
    assert "no remote" in check_publishable(repo)


def test_an_unauthenticated_cli_is_refused(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "is_authenticated", lambda _root: False)
    assert "not authenticated" in check_publishable(repo)


def test_a_repository_that_moved_since_verification_is_refused(repo: Path) -> None:
    """The window between verifying and writing is where a repository can change."""
    outcome = outcome_for(repo)
    (repo / "app.py").write_text("something else entirely\n", encoding="utf-8")
    run(repo, "commit", "-am", "changed underneath")
    result = publish(outcome, PublishRequest(repository=repo))
    assert result.status is PublishStatus.REFUSED
    assert "changed since the patch was verified" in result.refusal


# ------------------------------------------------------------ publishing ---


def test_publishing_commits_only_the_patched_files(repo: Path) -> None:
    result = publish(outcome_for(repo), PublishRequest(repository=repo))

    assert result.status is PublishStatus.PUBLISHED
    assert result.commit is not None
    assert result.commit.files == ("app.py",)
    assert run(repo, "show", "--name-only", "--format=", result.commit.sha) == "app.py"


def test_publishing_leaves_the_user_on_their_original_branch(repo: Path) -> None:
    result = publish(outcome_for(repo), PublishRequest(repository=repo))
    assert git.current_branch(repo) == "main"
    assert git.branch_exists(repo, result.branch)


def test_the_branch_name_carries_the_run_id(repo: Path) -> None:
    result = publish(outcome_for(repo), PublishRequest(repository=repo))
    assert result.branch.startswith("rewire/")
    assert "abc123" in result.branch


def test_the_branch_prefix_can_be_chosen(repo: Path) -> None:
    result = publish(outcome_for(repo), PublishRequest(repository=repo, prefix="bot"))
    assert result.branch.startswith("bot/")


def test_the_pull_request_targets_the_default_branch(
    repo: Path, stub_github: list[dict[str, object]]
) -> None:
    publish(outcome_for(repo), PublishRequest(repository=repo))
    assert stub_github[0]["base"] == "main"


def test_the_base_branch_can_be_overridden(
    repo: Path, stub_github: list[dict[str, object]]
) -> None:
    publish(outcome_for(repo), PublishRequest(repository=repo, base="develop"))
    assert stub_github[0]["base"] == "develop"


def test_a_draft_is_requested_when_asked(repo: Path, stub_github: list[dict[str, object]]) -> None:
    publish(outcome_for(repo), PublishRequest(repository=repo, draft=True))
    assert stub_github[0]["draft"] is True


def test_the_outcome_carries_the_pull_request(repo: Path) -> None:
    result = publish(outcome_for(repo), PublishRequest(repository=repo))
    assert result.published
    assert result.pull_request is not None
    assert result.pull_request.number == 7
    assert "#7" in result.pull_request.describe()


# --------------------------------------------------------------- dry run ---


def test_a_dry_run_commits_but_never_pushes(
    repo: Path, stub_github: list[dict[str, object]]
) -> None:
    result = publish(outcome_for(repo), PublishRequest(repository=repo, dry_run=True))
    assert result.status is PublishStatus.DRY_RUN
    assert result.commit is not None
    assert stub_github == []
    assert not result.published
    assert git.current_branch(repo) == "main"


def test_a_dry_run_still_produces_the_description(repo: Path) -> None:
    """So it can be read before anything is published."""
    result = publish(outcome_for(repo), PublishRequest(repository=repo, dry_run=True))
    assert result.title
    assert "What this does not establish" in result.body


# ----------------------------------------------------------- description ---


def test_the_title_names_the_specification_and_version(repo: Path) -> None:
    assert build_title(outcome_for(repo)) == "Migrate Example API to 2"


def test_the_body_leads_with_what_rewire_cannot_do(repo: Path) -> None:
    body = build_body(outcome_for(repo))
    assert body.startswith("Rewire proposed this migration automatically.")
    assert "It cannot merge it" in body
    assert "has not been reviewed by a person" in body


def test_the_body_states_what_the_evidence_does_not_establish(repo: Path) -> None:
    """A description that lists only the green checks invites a skim."""
    body = build_body(outcome_for(repo))
    assert "## What this does not establish" in body
    assert "a migration can be wrong in a way no existing test exercises" in body
    assert "cannot catch a test whose *expected values* were rewritten" in body


def test_the_body_shows_the_checks_before_and_after(repo: Path) -> None:
    body = build_body(outcome_for(repo))
    assert "| Check | Tool | Before | After |" in body
    assert "| tests | `pytest` | passed | passed |" in body


def test_the_body_names_the_api_changes(repo: Path) -> None:
    body = build_body(outcome_for(repo))
    assert "## What changed in the API" in body
    assert "`1` → `2`" in body


def test_the_body_reports_the_cost(repo: Path) -> None:
    body = build_body(outcome_for(repo))
    assert "## Cost" in body
    assert "1 attempt(s)" in body


def test_the_body_ends_by_saying_nothing_will_merge_it(repo: Path) -> None:
    body = build_body(outcome_for(repo))
    assert "no merge, approve or auto-merge capability" in body
    assert "abc123" in body


def test_the_body_surfaces_non_blocking_findings(repo: Path) -> None:
    """An inversion does not withhold the verdict, and a reviewer still wants it."""
    from rewire.analyzers.weakening import Weakening, WeakeningKind

    outcome = outcome_for(repo)
    attempt = outcome.repair.attempts[0]  # type: ignore[union-attr]
    noted = attempt.report.model_copy(  # type: ignore[union-attr]
        update={
            "weakenings": (
                Weakening(
                    kind=WeakeningKind.ASSERTION_INVERTED,
                    file="tests/test_app.py",
                    test="test_x",
                    before=0,
                    after=1,
                ),
            )
        }
    )
    outcome = outcome.repair.attempts[0]  # type: ignore[union-attr]
    body = build_body(
        MigrationOutcome(
            run_id="abc123",
            status=MigrationStatus.VERIFIED,
            changes=None,
            repair=RepairOutcome(
                attempts=(Attempt(number=1, result=outcome.result, report=noted),),
                stopped_because="verified",
            ),
        )
    )
    assert "did not block the verdict but is" in body
    assert "asserts absence" in body


# ---------------------------------------------------------- what is absent ---


def test_there_is_no_way_to_merge_anything() -> None:
    """The guarantee is structural: the capability does not exist to be reached.

    Checked over the GitHub module's string literals, so a future flag cannot
    quietly acquire one without this failing.
    """
    tree = ast.parse(Path("src/rewire/gitio/github.py").read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } - docstrings
    forbidden = {"merge", "--auto", "--merge", "--squash", "--rebase", "review", "--approve"}
    assert not (literals & forbidden), literals & forbidden
