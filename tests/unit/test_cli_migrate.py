"""Tests for `rewire migrate`, the one command that can modify a repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rewire.cli import app
from rewire.llm import ScriptBuilder, ScriptedProvider
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
def _quiet_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REWIRE_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("REWIRE_DATA_DIR", str(tmp_path / ".rewire"))
    from rewire.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


@pytest.fixture
def case(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nname="a"\nversion="1"\ndependencies=["openai"]\n', encoding="utf-8"
    )
    (repo / "app.py").write_text(CLIENT, encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
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


def use_sandbox(monkeypatch: pytest.MonkeyPatch, patched_exit: int) -> None:
    """Script the sandbox: baseline always passes, the patched run is chosen."""
    sandbox = ScriptedRunner()
    for _ in range(4):
        sandbox.when("-m pytest", exit_code=0, times=1)
        sandbox.when("-m pytest", exit_code=patched_exit, stdout="1 failed", times=1)

    def patched(repository: Path, patch: object = None, **kwargs: object) -> object:
        kwargs.pop("runner_factory", None)
        return real_verify(
            repository,
            patch,  # type: ignore[arg-type]
            runner_factory=lambda _root, _request: sandbox,
            **kwargs,  # type: ignore[arg-type]
        )

    monkeypatch.setattr("rewire.services.repair.verify", patched)


def use_provider(monkeypatch: pytest.MonkeyPatch, provider: ScriptedProvider) -> None:
    monkeypatch.setattr("rewire.cli.build_provider", lambda _settings: provider)


def editing_provider(times: int = 3) -> ScriptedProvider:
    builder = ScriptBuilder()
    for _ in range(times):
        builder = builder.calls(
            "propose_edit",
            file="app.py",
            old_text="max_tokens=100",
            new_text="max_completion_tokens=100",
            rationale="renamed",
        ).says("Renamed the request field.")
    return builder.build()


def migrate(case: Path, *extra: str) -> object:
    return runner.invoke(
        app,
        [
            "migrate",
            str(case / "repo"),
            "--old",
            str(case / "old.yaml"),
            "--new",
            str(case / "new.yaml"),
            *extra,
        ],
    )


def test_a_verified_migration_reports_without_writing(
    case: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_provider(monkeypatch, editing_provider())
    use_sandbox(monkeypatch, patched_exit=0)
    result = migrate(case, "--no-diff")
    assert result.exit_code == 0
    assert "VERIFIED" in result.stdout
    assert (case / "repo" / "app.py").read_text(encoding="utf-8") == CLIENT


def test_apply_writes_the_patch_and_says_how_to_undo_it(
    case: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_provider(monkeypatch, editing_provider())
    use_sandbox(monkeypatch, patched_exit=0)
    result = migrate(case, "--apply", "--no-diff")
    assert result.exit_code == 0
    assert "APPLIED" in result.stdout
    assert "git checkout --" in result.stdout
    assert "max_completion_tokens" in (case / "repo" / "app.py").read_text(encoding="utf-8")


def test_an_unverified_patch_is_never_written(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no flag to override this, and the command fails."""
    use_provider(monkeypatch, editing_provider())
    use_sandbox(monkeypatch, patched_exit=1)
    result = migrate(case, "--apply", "--no-diff")
    assert result.exit_code == 1
    assert "UNVERIFIED" in result.stdout
    assert (case / "repo" / "app.py").read_text(encoding="utf-8") == CLIENT


def test_a_dirty_tree_refuses_the_write(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (case / "repo" / "notes.txt").write_text("work in progress\n", encoding="utf-8")
    use_provider(monkeypatch, editing_provider())
    use_sandbox(monkeypatch, patched_exit=0)
    result = migrate(case, "--apply", "--no-diff")
    assert result.exit_code == 1
    assert "REFUSED" in result.stdout
    assert (case / "repo" / "app.py").read_text(encoding="utf-8") == CLIENT


def test_a_dirty_tree_can_be_overridden(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (case / "repo" / "notes.txt").write_text("work in progress\n", encoding="utf-8")
    use_provider(monkeypatch, editing_provider())
    use_sandbox(monkeypatch, patched_exit=0)
    result = migrate(case, "--apply", "--allow-dirty", "--no-diff")
    assert result.exit_code == 0
    assert "APPLIED" in result.stdout


def test_a_specification_with_nothing_breaking_succeeds_quietly(
    case: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (case / "new.yaml").write_text(SPEC.format(v="2", field="max_tokens"), encoding="utf-8")
    use_provider(monkeypatch, editing_provider())
    result = migrate(case, "--apply")
    assert result.exit_code == 0
    assert "NO BREAKING CHANGES" in result.stdout


def test_the_diff_can_be_written_to_a_file(
    case: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_provider(monkeypatch, editing_provider())
    use_sandbox(monkeypatch, patched_exit=0)
    target = tmp_path / "out.patch"
    migrate(case, "--write-diff", str(target), "--no-diff")
    assert "max_completion_tokens" in target.read_text(encoding="utf-8")


def test_the_attempt_table_is_shown(case: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_provider(monkeypatch, editing_provider())
    use_sandbox(monkeypatch, patched_exit=0)
    assert "Attempts" in migrate(case, "--no-diff").stdout
