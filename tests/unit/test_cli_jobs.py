"""Tests for `rewire jobs` and `rewire worker`.

The queue and the worker are tested directly in ``test_jobs`` and
``test_worker``. What is covered here is the layer a person touches: that
submitting returns without running anything, that the worker command is wired to
a runtime that cannot write, and that a job carries no permission however it was
submitted.

The worker command builds a real provider, so the tests that reach it replace
that -- the point of the test is the wiring, not the model.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from rewire import cli
from rewire.cli import app
from rewire.core.config import get_settings
from rewire.jobs import JobState, JobStore
from rewire.jobs.models import Job

runner = CliRunner()

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Catalog API", "version": "1.0.0"},
    "paths": {
        "/items": {
            "get": {
                "operationId": "listItems",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"sku": {"type": "string"}},
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}


def flat(text: str) -> str:
    """Collapse Rich's line wrapping, so a prose assertion is not a layout assertion."""
    return " ".join(text.split())


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("REWIRE_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("REWIRE_DATA_DIR", str(tmp_path / ".rewire"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "repo").mkdir()
    (tmp_path / "old.yaml").write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    (tmp_path / "new.yaml").write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    return tmp_path


def store(project: Path) -> JobStore:
    return JobStore(project / ".rewire" / "jobs.db")


def submit(project: Path, *extra: str) -> str:
    result = runner.invoke(
        app,
        [
            "jobs",
            "submit",
            str(project / "repo"),
            "--old",
            str(project / "old.yaml"),
            "--new",
            str(project / "new.yaml"),
            *extra,
        ],
    )
    assert result.exit_code == 0, result.output
    return flat(result.output).split("queued ")[1].split(" ")[0]


# ------------------------------------------------------------------ submit ---


def test_submitting_returns_without_running_anything(project: Path) -> None:
    """The reason this is a queue: the caller is not held for two minutes."""
    job_id = submit(project)
    queued = store(project).get(job_id)
    assert queued.state is JobState.QUEUED
    assert queued.attempts == 0


def test_a_submitted_job_carries_absolute_paths(project: Path) -> None:
    """A worker is a different process with a different working directory."""
    payload = store(project).get(submit(project)).payload
    assert Path(str(payload["repository"])).is_absolute()
    assert Path(str(payload["old_spec"])).is_absolute()


def test_a_submitted_job_carries_no_permission_to_write(project: Path) -> None:
    """There is no flag to grant one, and no field on the payload to hold it."""
    payload = store(project).get(submit(project)).payload
    assert set(payload) == {"repository", "old_spec", "new_spec", "packages"}


def test_packages_reach_the_payload(project: Path) -> None:
    payload = store(project).get(submit(project, "--package", "acme")).payload
    assert payload["packages"] == ["acme"]


def test_submitting_a_missing_repository_is_refused(project: Path) -> None:
    result = runner.invoke(
        app,
        [
            "jobs",
            "submit",
            str(project / "absent"),
            "--old",
            str(project / "old.yaml"),
            "--new",
            str(project / "new.yaml"),
        ],
    )
    assert result.exit_code != 0


# -------------------------------------------------------------------- list ---


def test_listing_an_empty_queue_says_so(project: Path) -> None:
    result = runner.invoke(app, ["jobs", "list"])
    assert result.exit_code == 0
    assert "No jobs" in flat(result.output)


def test_listing_shows_every_state_including_the_empty_ones(project: Path) -> None:
    """A count that omits 'FAILED 0' makes zero look like missing."""
    submit(project)
    output = flat(runner.invoke(app, ["jobs", "list"]).output)
    assert "QUEUED 1" in output
    assert "FAILED 0" in output


def test_listing_can_be_filtered_by_state(project: Path) -> None:
    submit(project)
    result = runner.invoke(app, ["jobs", "list", "--state", "running"])
    assert result.exit_code == 0
    assert "No jobs" in flat(result.output)


# -------------------------------------------------------------------- show ---


def test_showing_a_job_prints_its_payload(project: Path) -> None:
    job_id = submit(project)
    result = runner.invoke(app, ["jobs", "show", job_id])
    assert result.exit_code == 0
    assert "QUEUED" in flat(result.output)
    assert "old.yaml" in result.output


def test_showing_a_finished_job_prints_the_run_record(project: Path) -> None:
    """The queue answers 'what happened' from the record the run wrote."""
    from rewire.services.migrate import MigrationOutcome, MigrationStatus
    from rewire.services.record import write_record

    job_id = submit(project)
    queue = store(project)
    claimed = queue.claim("w")
    assert claimed is not None
    write_record(
        project / ".rewire" / "runs",
        MigrationOutcome(run_id="run-xyz", status=MigrationStatus.VERIFIED),
    )
    queue.succeed(job_id, run_id="run-xyz", worker="w")

    result = runner.invoke(app, ["jobs", "show", job_id])
    assert result.exit_code == 0
    assert "run-xyz" in result.output
    assert "verified" in result.output


def test_a_finished_job_whose_record_is_gone_says_so(project: Path) -> None:
    """Reporting the job and admitting the record is missing beats crashing."""
    job_id = submit(project)
    queue = store(project)
    claimed = queue.claim("w")
    assert claimed is not None
    queue.succeed(job_id, run_id="run-never-written", worker="w")

    result = runner.invoke(app, ["jobs", "show", job_id])
    assert result.exit_code == 0
    assert "no run record" in flat(result.output)


def test_showing_an_unknown_job_is_a_readable_error(project: Path) -> None:
    result = runner.invoke(app, ["jobs", "show", "absent"])
    assert result.exit_code != 0


# ------------------------------------------------------------------ cancel ---


def test_a_queued_job_can_be_withdrawn(project: Path) -> None:
    job_id = submit(project)
    result = runner.invoke(app, ["jobs", "cancel", job_id])
    assert result.exit_code == 0
    assert store(project).get(job_id).state is JobState.CANCELLED


def test_cancelling_a_finished_job_is_a_readable_error(project: Path) -> None:
    job_id = submit(project)
    queue = store(project)
    assert queue.claim("w") is not None
    queue.succeed(job_id, run_id="r", worker="w")
    assert runner.invoke(app, ["jobs", "cancel", job_id]).exit_code != 0


# ------------------------------------------------------------------ worker ---


def test_the_worker_drains_the_queue_it_was_pointed_at(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the command, with the migration itself replaced."""
    from rewire.jobs import worker as worker_module
    from rewire.services.migrate import MigrationOutcome, MigrationStatus

    monkeypatch.setattr(cli, "build_provider", lambda _settings: object())
    monkeypatch.setattr(
        worker_module,
        "run_migration",
        lambda request, *, runtime, run_id="": MigrationOutcome(
            run_id=run_id, status=MigrationStatus.VERIFIED
        ),
    )
    job_id = submit(project)

    result = runner.invoke(app, ["worker", "--name", "w1", "--max-jobs", "1", "--poll", "0.1"])
    assert result.exit_code == 0
    assert "after 1 job(s)" in flat(result.output)
    assert store(project).get(job_id).state is JobState.SUCCEEDED


def test_the_worker_cannot_write_to_a_working_tree(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property that makes queued work safe to leave running.

    Captured from the request the pipeline was actually handed, rather than from
    the worker's configuration, so this fails if anything reconnects the payload
    to the policy.
    """
    from rewire.jobs import worker as worker_module
    from rewire.services.migrate import MigrationOutcome, MigrationStatus

    seen: list[Job] = []

    def capture(request: object, *, runtime: object, run_id: str = "") -> MigrationOutcome:
        seen.append(request)  # type: ignore[arg-type]
        return MigrationOutcome(run_id=run_id, status=MigrationStatus.VERIFIED)

    monkeypatch.setattr(cli, "build_provider", lambda _settings: object())
    monkeypatch.setattr(worker_module, "run_migration", capture)
    submit(project)

    runner.invoke(app, ["worker", "--max-jobs", "1", "--poll", "0.1"])
    assert seen
    assert seen[0].apply is False  # type: ignore[attr-defined]
    assert seen[0].allow_dirty is False  # type: ignore[attr-defined]


def test_a_worker_with_nothing_to_do_exits_when_bounded(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "build_provider", lambda _settings: object())
    import threading

    from rewire.jobs.worker import Worker

    original = Worker.run_forever

    def stop_soon(self: Worker, **kwargs: object) -> int:
        threading.Timer(0.05, self.stop).start()
        return original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Worker, "run_forever", stop_soon)
    result = runner.invoke(app, ["worker", "--poll", "0.1"])
    assert result.exit_code == 0
    assert "after 0 job(s)" in flat(result.output)
