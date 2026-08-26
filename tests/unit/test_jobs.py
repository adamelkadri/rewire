"""Tests for the job queue, against a real SQLite file and real threads.

A mocked database would prove the code calls the functions it calls. What has to
be true here is stronger and is a property of the database: that two workers
racing for one job produce one winner, that a worker which dies holding a job
does not strand it, and that a job which keeps killing its worker is eventually
stopped rather than handed out forever.

Time is injected everywhere so lease expiry can be tested in microseconds rather
than by sleeping through it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from rewire.jobs.models import Job, JobState, utc_now
from rewire.jobs.store import JobError, JobStore, open_store

NOW = utc_now()


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.db", lease_seconds=60, max_attempts=3)


# ------------------------------------------------------------------- basics ---


def test_a_submitted_job_waits_to_be_claimed(store: JobStore) -> None:
    job = store.submit("migrate", {"repository": "/repo"})
    assert job.state is JobState.QUEUED
    assert job.attempts == 0
    assert store.get(job.id).payload == {"repository": "/repo"}


def test_a_job_survives_the_process_that_created_it(tmp_path: Path) -> None:
    """The whole point of a durable queue: a restart is not a data loss."""
    first = JobStore(tmp_path / "jobs.db")
    job = first.submit("migrate", {"repository": "/repo"})

    reopened = open_store(tmp_path / "jobs.db")
    assert reopened.get(job.id).state is JobState.QUEUED
    assert reopened.get(job.id).payload == {"repository": "/repo"}


def test_claiming_an_empty_queue_returns_nothing(store: JobStore) -> None:
    assert store.claim("worker-1") is None


def test_a_claim_marks_the_job_running_and_counts_the_attempt(store: JobStore) -> None:
    store.submit("migrate")
    claimed = store.claim("worker-1", now=NOW)
    assert claimed is not None
    assert claimed.state is JobState.RUNNING
    assert claimed.worker == "worker-1"
    assert claimed.attempts == 1
    assert claimed.lease_expires_at == NOW + timedelta(seconds=60)


def test_jobs_are_claimed_oldest_first(store: JobStore) -> None:
    first = store.submit("migrate", {"n": 1})
    second = store.submit("migrate", {"n": 2})
    assert store.claim("w", now=NOW) is not None
    taken = [store.get(first.id).state, store.get(second.id).state]
    assert taken == [JobState.RUNNING, JobState.QUEUED]


def test_an_unknown_job_is_a_readable_error(store: JobStore) -> None:
    with pytest.raises(JobError, match="no job with that identifier"):
        store.get("absent")
    assert store.find("absent") is None


# -------------------------------------------------------------- concurrency ---


def test_two_workers_racing_for_one_job_produce_one_winner(tmp_path: Path) -> None:
    """The property the guarded UPDATE exists for. Real threads, real database."""
    store = JobStore(tmp_path / "jobs.db")
    store.submit("migrate")

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda n: store.claim(f"worker-{n}"), range(8)))

    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert store.get(winners[0].id).attempts == 1


def test_every_job_is_claimed_exactly_once_under_contention(tmp_path: Path) -> None:
    """Ten jobs, eight workers, each job handed out once and no job lost."""
    store = JobStore(tmp_path / "jobs.db")
    for index in range(10):
        store.submit("migrate", {"n": index})

    def drain(worker: int) -> list[str]:
        taken: list[str] = []
        while (job := store.claim(f"worker-{worker}")) is not None:
            taken.append(job.id)
        return taken

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = [job_id for batch in pool.map(drain, range(8)) for job_id in batch]

    assert len(claimed) == 10
    assert len(set(claimed)) == 10
    assert store.counts()[JobState.RUNNING] == 10


# ------------------------------------------------------------------- leases ---


def test_a_heartbeat_extends_the_claim(store: JobStore) -> None:
    store.submit("migrate")
    claimed = store.claim("worker-1", now=NOW)
    assert claimed is not None

    later = NOW + timedelta(seconds=30)
    assert store.heartbeat(claimed.id, "worker-1", now=later) is True
    assert store.get(claimed.id).lease_expires_at == later + timedelta(seconds=60)


def test_a_heartbeat_from_a_worker_that_lost_the_lease_says_so(store: JobStore) -> None:
    """It should stop: finishing would write a result for work being redone."""
    store.submit("migrate")
    claimed = store.claim("worker-1", now=NOW)
    assert claimed is not None
    assert store.heartbeat(claimed.id, "worker-2", now=NOW) is False


def test_an_expired_lease_returns_the_job_to_the_queue(store: JobStore) -> None:
    """The only path by which a killed worker's job runs again."""
    store.submit("migrate")
    claimed = store.claim("worker-1", now=NOW)
    assert claimed is not None

    expired = NOW + timedelta(seconds=61)
    recovered = store.recover_expired(now=expired)
    assert [job.state for job in recovered] == [JobState.QUEUED]

    requeued = store.get(claimed.id)
    assert requeued.state is JobState.QUEUED
    assert requeued.worker == ""
    assert requeued.attempts == 1


def test_a_live_lease_is_left_alone(store: JobStore) -> None:
    store.submit("migrate")
    claimed = store.claim("worker-1", now=NOW)
    assert claimed is not None
    assert store.recover_expired(now=NOW + timedelta(seconds=59)) == ()
    assert store.get(claimed.id).state is JobState.RUNNING


def test_claiming_recovers_stale_jobs_first(store: JobStore) -> None:
    """A worker asking for work is the natural moment to notice a dead one."""
    store.submit("migrate")
    first = store.claim("worker-1", now=NOW)
    assert first is not None

    second = store.claim("worker-2", now=NOW + timedelta(seconds=61))
    assert second is not None
    assert second.id == first.id
    assert second.worker == "worker-2"
    assert second.attempts == 2


def test_a_job_that_keeps_killing_its_worker_is_stopped(store: JobStore) -> None:
    """Otherwise one poison job takes down a worker every lease period."""
    store.submit("migrate")
    moment = NOW
    for _ in range(3):
        assert store.claim("worker", now=moment) is not None
        moment += timedelta(seconds=61)

    store.recover_expired(now=moment)
    abandoned = store.list_jobs()[0]
    assert abandoned.state is JobState.FAILED
    assert abandoned.attempts == 3
    assert "abandoned by its worker" in abandoned.error
    assert store.claim("worker", now=moment) is None


# ------------------------------------------------------------------ endings ---


def test_a_finished_job_names_the_run_it_produced(store: JobStore) -> None:
    store.submit("migrate")
    claimed = store.claim("worker-1", now=NOW)
    assert claimed is not None

    done = store.succeed(claimed.id, run_id="run-abc", now=NOW)
    assert done.state is JobState.SUCCEEDED
    assert done.run_id == "run-abc"
    assert done.worker == ""
    assert done.lease_expires_at is None


def test_a_failed_job_is_not_retried_by_the_lease(store: JobStore) -> None:
    """A caught exception knows something an expiry does not: it concluded."""
    store.submit("migrate")
    claimed = store.claim("worker-1", now=NOW)
    assert claimed is not None

    store.fail(claimed.id, "docker is not running", now=NOW)
    assert store.recover_expired(now=NOW + timedelta(days=1)) == ()
    assert store.claim("worker-2", now=NOW + timedelta(days=1)) is None
    assert store.get(claimed.id).error == "docker is not running"


@pytest.mark.parametrize("ending", ["succeed", "fail"])
def test_finishing_a_job_that_is_not_running_is_refused(store: JobStore, ending: str) -> None:
    job = store.submit("migrate")
    with pytest.raises(JobError, match="not running"):
        store.succeed(job.id) if ending == "succeed" else store.fail(job.id, "why")


def test_a_queued_job_can_be_withdrawn(store: JobStore) -> None:
    job = store.submit("migrate")
    assert store.cancel(job.id, now=NOW).state is JobState.CANCELLED
    assert store.claim("worker-1") is None


def test_a_running_job_is_cancelled_where_it_stands(store: JobStore) -> None:
    """Nothing here can reach into another process, and it does not pretend to."""
    store.submit("migrate")
    claimed = store.claim("worker-1", now=NOW)
    assert claimed is not None
    assert store.cancel(claimed.id, now=NOW).state is JobState.CANCELLED


def test_a_finished_job_cannot_be_cancelled(store: JobStore) -> None:
    store.submit("migrate")
    claimed = store.claim("worker-1", now=NOW)
    assert claimed is not None
    store.succeed(claimed.id, run_id="r", now=NOW)
    with pytest.raises(JobError, match="already succeeded"):
        store.cancel(claimed.id)


def test_cancelling_an_unknown_job_is_a_readable_error(store: JobStore) -> None:
    with pytest.raises(JobError, match="no job with that identifier"):
        store.cancel("absent")


# ------------------------------------------------------------------ reading ---


def test_jobs_can_be_listed_by_state_and_kind(store: JobStore) -> None:
    store.submit("migrate", {"n": 1})
    store.submit("watch", {"n": 2})
    claimed = store.claim("worker-1", now=NOW)
    assert claimed is not None

    assert len(store.list_jobs()) == 2
    assert [job.kind for job in store.list_jobs(kind="watch")] == ["watch"]
    running = store.list_jobs(states=[JobState.RUNNING])
    assert [job.id for job in running] == [claimed.id]
    assert store.list_jobs(limit=1) != ()


def test_counts_include_the_states_with_nothing_in_them(store: JobStore) -> None:
    """A dashboard that omits 'failed: 0' makes zero look like missing."""
    store.submit("migrate")
    counts = store.counts()
    assert set(counts) == set(JobState)
    assert counts[JobState.QUEUED] == 1
    assert counts[JobState.FAILED] == 0


# ------------------------------------------------------------------- states ---


def test_the_three_endings_are_endings() -> None:
    assert JobState.SUCCEEDED.is_finished
    assert JobState.FAILED.is_finished
    assert JobState.CANCELLED.is_finished
    assert not JobState.QUEUED.is_finished
    assert not JobState.RUNNING.is_finished
    assert JobState.QUEUED.is_claimable
    assert not JobState.RUNNING.is_claimable


def test_only_a_running_job_can_have_a_stale_lease() -> None:
    queued = Job(id="a", kind="migrate")
    assert queued.lease_expired(now=NOW + timedelta(days=1)) is False
    assert queued.is_finished is False

    running = Job(
        id="b",
        kind="migrate",
        state=JobState.RUNNING,
        lease_expires_at=NOW,
    )
    assert running.lease_expired(now=NOW + timedelta(seconds=1)) is True
    assert running.lease_expired(now=NOW - timedelta(seconds=1)) is False
    assert Job(id="c", kind="migrate", state=JobState.RUNNING).lease_expired() is False


@pytest.mark.parametrize("state", list(JobState))
def test_every_state_describes_itself(state: JobState) -> None:
    job = Job(id="abc", kind="migrate", state=state, worker="w", run_id="r", error="boom")
    assert job.describe()


def test_a_default_lease_is_used_when_none_is_given(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    store.submit("migrate")
    claimed = store.claim("worker-1", now=NOW)
    assert claimed is not None
    assert claimed.lease_expires_at is not None
    assert claimed.lease_expires_at > NOW
