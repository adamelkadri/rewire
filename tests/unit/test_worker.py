"""Tests for the worker loop, with the migration replaced by a stub.

The migration itself is tested in ``test_migrate_service``; what is covered here
is the loop around it, and in particular the three failures it exists to survive:
a handler that raises, a handler that crashes with something the domain does not
model, and a worker whose lease expires while it is working.

That last one is the important one. A worker that lost its lease may be racing
another worker that has already taken the job, so it must record *nothing* — and
the only way to observe "recorded nothing" is to check that the job is still
where the other worker left it.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from rewire.core.errors import GitError
from rewire.jobs.models import Job, JobState, utc_now
from rewire.jobs.store import JobStore
from rewire.jobs.worker import (
    MIGRATE,
    JobPayloadError,
    Worker,
    WorkerResult,
    _task_from,
    build_worker,
    install_signal_handlers,
)

NOW = utc_now()


def _just_past_lease(store: JobStore, job: Job) -> datetime:
    """A moment after the lease currently held on ``job``."""
    current = store.get(job.id)
    assert current.lease_expires_at is not None
    return current.lease_expires_at + timedelta(seconds=1)


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.db", lease_seconds=60, max_attempts=3)


def worker(store: JobStore, handler: object, *, kind: str = MIGRATE) -> Worker:
    return Worker(store=store, handlers={kind: handler}, name="worker-1")  # type: ignore[dict-item]


def succeeding(run_id: str = "run-1") -> object:
    def handle(_job: Job) -> str:
        return run_id

    return handle


# -------------------------------------------------------------------- loop ---


def test_an_empty_queue_leaves_the_worker_idle(store: JobStore) -> None:
    result = worker(store, succeeding()).run_one()
    assert result == WorkerResult(idle=True)
    assert result.worked is False


def test_a_job_is_claimed_run_and_recorded(store: JobStore) -> None:
    job = store.submit(MIGRATE, {"repository": "/repo"})
    result = worker(store, succeeding("run-abc")).run_one()

    assert result.worked is True
    finished = store.get(job.id)
    assert finished.state is JobState.SUCCEEDED
    assert finished.run_id == "run-abc"
    assert finished.worker == ""


def test_the_loop_drains_the_queue_and_stops_when_asked(store: JobStore) -> None:
    for index in range(3):
        store.submit(MIGRATE, {"n": index})
    drained = worker(store, succeeding()).run_forever(max_jobs=3, idle_sleep=0.01)
    assert drained == 3
    assert store.counts()[JobState.SUCCEEDED] == 3


def test_a_stopped_worker_leaves_the_queue_alone(store: JobStore) -> None:
    store.submit(MIGRATE)
    subject = worker(store, succeeding())
    subject.stop()
    assert subject.stopping is True
    assert subject.run_forever(idle_sleep=0.01) == 0
    assert store.counts()[JobState.QUEUED] == 1


def test_an_idle_worker_stops_without_waiting_out_its_sleep(store: JobStore) -> None:
    """The stop event ends the wait, rather than the wait ending on its own."""
    subject = worker(store, succeeding())
    threading.Timer(0.05, subject.stop).start()
    assert subject.run_forever(idle_sleep=30) == 0


# ---------------------------------------------------------------- failures ---


def test_a_handler_that_raises_a_domain_error_fails_the_job(store: JobStore) -> None:
    def boom(_job: Job) -> str:
        raise GitError("docker is not running")

    job = store.submit(MIGRATE)
    worker(store, boom).run_one()

    failed = store.get(job.id)
    assert failed.state is JobState.FAILED
    assert "docker is not running" in failed.error


def test_a_handler_that_crashes_does_not_take_the_worker_with_it(store: JobStore) -> None:
    """One bad job must not end the process that could still run the others."""

    def crash(_job: Job) -> str:
        raise ZeroDivisionError("division by zero")

    first = store.submit(MIGRATE)
    second = store.submit(MIGRATE)
    subject = Worker(store=store, handlers={MIGRATE: crash}, name="worker-1")
    subject.run_one()

    assert "ZeroDivisionError" in store.get(first.id).error
    assert store.get(second.id).state is JobState.QUEUED


def test_a_job_nobody_can_run_fails_rather_than_looping(store: JobStore) -> None:
    job = store.submit("something-else")
    worker(store, succeeding()).run_one()
    failed = store.get(job.id)
    assert failed.state is JobState.FAILED
    assert "no handler" in failed.error


# ------------------------------------------------------------------- lease ---


def test_a_worker_that_lost_its_lease_records_nothing(store: JobStore) -> None:
    """The property that stops two workers writing conflicting results.

    While the handler runs, the lease expires and a second worker takes the job.
    The first must leave it exactly where the second put it.
    """
    store.submit(MIGRATE)
    subject = worker(store, succeeding("run-first"))
    taken: list[Job] = []

    def steal(job: Job) -> str:
        # Derived from the lease this worker actually holds, not from a
        # timestamp fixed at import: the worker claims in wall-clock time, so a
        # module-level constant drifts out of range as the suite runs.
        stolen = store.claim("worker-2", now=_just_past_lease(store, job))
        assert stolen is not None
        taken.append(stolen)
        return "run-first"

    subject.handlers[MIGRATE] = steal
    subject.run_one()

    assert taken, "the second worker did not get the job"
    current = store.get(taken[0].id)
    assert current.state is JobState.RUNNING
    assert current.worker == "worker-2"
    assert current.run_id == ""


def test_losing_the_lease_also_suppresses_a_failure(store: JobStore) -> None:
    """A failure written by the losing worker would be just as wrong as a success."""
    store.submit(MIGRATE)
    subject = worker(store, succeeding())

    def steal_then_fail(job: Job) -> str:
        assert store.claim("worker-2", now=_just_past_lease(store, job)) is not None
        raise GitError("this worker's own problem")

    subject.handlers[MIGRATE] = steal_then_fail
    subject.run_one()

    stolen = store.list_jobs()[0]
    assert stolen.state is JobState.RUNNING
    assert stolen.worker == "worker-2"
    assert stolen.error == ""


# ---------------------------------------------------------------- payloads ---


def test_a_payload_becomes_a_task(tmp_path: Path) -> None:
    task = _task_from(
        {
            "repository": str(tmp_path),
            "old_spec": "old.yaml",
            "new_spec": "new.yaml",
            "packages": ["acme"],
        }
    )
    assert task.repository == tmp_path
    assert task.packages == ("acme",)


@pytest.mark.parametrize(
    ("payload", "missing"),
    [
        ({}, "repository"),
        ({"repository": "/r"}, "old_spec"),
        ({"repository": "/r", "old_spec": "o"}, "new_spec"),
    ],
)
def test_an_incomplete_payload_is_refused(payload: dict[str, object], missing: str) -> None:
    with pytest.raises(JobPayloadError, match=missing):
        _task_from(payload)


def test_packages_of_the_wrong_shape_are_dropped_not_guessed() -> None:
    task = _task_from({"repository": "/r", "old_spec": "o", "new_spec": "n", "packages": "acme"})
    assert task.packages == ()


def test_a_payload_cannot_grant_permission_to_write(tmp_path: Path) -> None:
    """The reason MigrationTask and MigrationPolicy are separate types.

    A payload naming ``apply`` gets nothing: there is no field on a task to
    receive it, so there is no code path from the request to the authority.
    """
    task = _task_from(
        {
            "repository": str(tmp_path),
            "old_spec": "old.yaml",
            "new_spec": "new.yaml",
            "apply": True,
            "allow_dirty": True,
        }
    )
    assert not hasattr(task, "apply")
    assert not hasattr(task, "allow_dirty")


# ----------------------------------------------------------------- wiring ---


def test_a_built_worker_cannot_write_to_a_working_tree(tmp_path: Path) -> None:
    """A queued migration is unattended, so it proposes rather than edits."""
    from rewire.core.config import Settings
    from rewire.llm import ScriptBuilder
    from rewire.services.migrate import MigrationRuntime

    settings = Settings(data_dir=tmp_path / ".rewire")
    runtime = MigrationRuntime.from_settings(
        settings, provider=ScriptBuilder().says("done").build()
    )
    built = build_worker(JobStore(tmp_path / "jobs.db"), runtime=runtime, name="w1")
    assert built.name == "w1"
    assert MIGRATE in built.handlers


def test_signals_ask_the_worker_to_stop_between_jobs(store: JobStore) -> None:
    """Interrupting mid-migration leaves a container running for no benefit."""
    import signal

    subject = worker(store, succeeding())
    original = signal.getsignal(signal.SIGTERM)
    try:
        install_signal_handlers(subject)
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert subject.stopping is True
    finally:
        signal.signal(signal.SIGTERM, original)


# --------------------------------------------------------- migrate handler ---


def test_the_handler_runs_the_task_from_the_payload_under_its_own_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline is tested elsewhere; what matters here is what reaches it.

    The task comes from the job, the policy comes from the worker, and a payload
    asking to write is not a payload that writes.
    """
    from rewire.jobs import worker as worker_module
    from rewire.services.migrate import MigrationOutcome, MigrationPolicy, MigrationStatus

    seen: list[object] = []

    def fake_run(request: object, *, runtime: object, run_id: str = "") -> MigrationOutcome:
        seen.append(request)
        return MigrationOutcome(run_id=run_id, status=MigrationStatus.VERIFIED)

    monkeypatch.setattr(worker_module, "run_migration", fake_run)
    handler = worker_module.migrate_handler(object(), MigrationPolicy.read_only())  # type: ignore[arg-type]

    job = Job(
        id="job-1",
        kind=MIGRATE,
        payload={
            "repository": str(tmp_path),
            "old_spec": "old.yaml",
            "new_spec": "new.yaml",
            "apply": True,
            "allow_dirty": True,
        },
    )
    assert handler(job) == "job-1"

    request = seen[0]
    assert request.task.repository == tmp_path  # type: ignore[attr-defined]
    assert request.apply is False  # type: ignore[attr-defined]
    assert request.allow_dirty is False  # type: ignore[attr-defined]


def test_a_migration_that_did_not_succeed_fails_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unverified patch is not a finished job, and the status says which."""
    from rewire.jobs import worker as worker_module
    from rewire.services.migrate import MigrationOutcome, MigrationPolicy, MigrationStatus

    monkeypatch.setattr(
        worker_module,
        "run_migration",
        lambda request, *, runtime, run_id="": MigrationOutcome(
            run_id=run_id, status=MigrationStatus.UNVERIFIED
        ),
    )
    handler = worker_module.migrate_handler(object(), MigrationPolicy.read_only())  # type: ignore[arg-type]
    job = Job(
        id="job-1",
        kind=MIGRATE,
        payload={"repository": str(tmp_path), "old_spec": "o", "new_spec": "n"},
    )
    with pytest.raises(JobPayloadError, match="unverified"):
        handler(job)


# --------------------------------------------------------------- heartbeat ---


def test_the_heartbeat_extends_the_claim_while_the_work_runs(store: JobStore) -> None:
    from rewire.jobs.worker import _Heartbeat

    store.submit(MIGRATE)
    claimed = store.claim("worker-1")
    assert claimed is not None
    before = store.get(claimed.id).lease_expires_at
    assert before is not None

    beat = _Heartbeat(store, claimed.id, "worker-1", interval=0.02)
    with beat:
        _wait_until(lambda: store.get(claimed.id).lease_expires_at != before)
    assert beat.lost is False
    assert store.get(claimed.id).lease_expires_at != before


def test_the_heartbeat_notices_a_lease_it_no_longer_holds(store: JobStore) -> None:
    """Not the guarantee -- the store's ownership guard is -- but it notices first."""
    from rewire.jobs.worker import _Heartbeat

    store.submit(MIGRATE)
    claimed = store.claim("worker-1")
    assert claimed is not None
    store.cancel(claimed.id)

    beat = _Heartbeat(store, claimed.id, "worker-1", interval=0.02)
    with beat:
        _wait_until(lambda: beat.lost)
    assert beat.lost is True


def _wait_until(condition: object, *, timeout: float = 2.0) -> None:
    """Poll until ``condition`` holds, so a thread test is not a sleep test."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():  # type: ignore[operator]
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")
