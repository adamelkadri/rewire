"""The process that drains the queue.

A worker is a loop: claim a job, run it, record what happened, repeat. Almost all
of the care here is in the three ways that loop can go wrong.

**A worker that loses its lease must not record a result.** The lease exists so
that a job held by a dead worker gets redone. A worker that was merely slow —
paused, swapped out, starved — comes back to find its lease expired and possibly
another worker already redoing the work. Writing a result at that point would
overwrite whatever the new owner concludes. So the lease is checked again before
recording, and a worker that lost it drops the result on the floor and says so.

**The payload is untrusted; the policy is not.** A job carries what to migrate,
which whoever submitted it is entitled to choose. It never carries permission to
write, which is the worker's configuration to grant. That separation is
:class:`~rewire.services.migrate.MigrationTask` against
:class:`~rewire.services.migrate.MigrationPolicy`, and the handler here is the
place it is enforced: a payload field named ``apply`` is simply not read.

**A worker stops between jobs, not during one.** A migration that is interrupted
half way leaves a container running and a patch unverified, and the queue would
redo it anyway once the lease expired. On a signal the worker finishes what it
holds and then exits, so the common case costs a couple of minutes and the
uncommon one is handled by the lease.
"""

from __future__ import annotations

import signal
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from rewire.core.errors import RewireError
from rewire.core.logging import get_logger
from rewire.jobs.models import Job
from rewire.jobs.store import JobStore, LeaseLostError
from rewire.services.migrate import (
    MigrationPolicy,
    MigrationRequest,
    MigrationRuntime,
    MigrationTask,
    run_migration,
)

logger = get_logger(__name__)

#: The job kind this worker knows how to run.
MIGRATE: Final[str] = "migrate"

#: Seconds to wait before asking an empty queue again. Short enough that a
#: submitted job starts promptly, long enough that an idle worker is not a
#: busy loop against the database.
IDLE_SLEEP_SECONDS: Final[float] = 1.0

#: Fraction of the lease at which to heartbeat. A third leaves room for two
#: missed beats before a live worker is mistaken for a dead one.
HEARTBEAT_FRACTION: Final[float] = 1 / 3

#: Runs a job and returns the run identifier it produced.
Handler = Callable[[Job], str]


class JobPayloadError(RewireError):
    """A job's payload does not describe work this worker can do."""

    code = "job_payload_error"


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """What one pass of the loop did, for a caller that drives it directly."""

    #: Jobs that reached a terminal state during this pass.
    finished: tuple[Job, ...] = ()
    #: Whether the queue was empty when the pass ended.
    idle: bool = False

    @property
    def worked(self) -> bool:
        """Whether the pass ran anything at all."""
        return bool(self.finished)


def migrate_handler(runtime: MigrationRuntime, policy: MigrationPolicy) -> Handler:
    """Build the handler for ``migrate`` jobs.

    The policy is captured here, from the worker's configuration, and the task is
    read from the job. A payload that contains ``apply`` or ``allow_dirty`` does
    not get them: there is no code path from the payload to the policy.

    Raises:
        JobPayloadError: through the returned handler, when the payload does not
            describe a migration.
    """

    def handle(job: Job) -> str:
        task = _task_from(job.payload)
        outcome = run_migration(
            MigrationRequest(task=task, policy=policy), runtime=runtime, run_id=job.id
        )
        if not outcome.status.is_success:
            raise JobPayloadError(
                f"the migration finished as {outcome.status.value}",
                job=job.id,
                summary=outcome.summary_line(),
            )
        return outcome.run_id

    return handle


def _task_from(payload: Mapping[str, object]) -> MigrationTask:
    """Read a migration task out of an untrusted payload.

    Raises:
        JobPayloadError: A required field is missing or the wrong shape.
    """
    missing = [key for key in ("repository", "old_spec", "new_spec") if not payload.get(key)]
    if missing:
        raise JobPayloadError(f"the payload is missing {', '.join(missing)}")
    packages = payload.get("packages") or []
    return MigrationTask(
        repository=Path(str(payload["repository"])),
        old_spec=Path(str(payload["old_spec"])),
        new_spec=Path(str(payload["new_spec"])),
        packages=tuple(str(item) for item in packages) if isinstance(packages, list) else (),
    )


@dataclass(slots=True)
class Worker:
    """Drains a queue until it is told to stop."""

    store: JobStore
    handlers: dict[str, Handler]
    name: str = field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:8]}")
    #: Set to stop claiming. The job in hand is always finished first. An event
    #: rather than a flag so the idle wait ends the moment it is set, instead of
    #: sleeping out its interval before noticing.
    _stop: threading.Event = field(default_factory=threading.Event)

    @property
    def stopping(self) -> bool:
        """Whether the worker has been asked to finish and exit."""
        return self._stop.is_set()

    def stop(self) -> None:
        """Ask the worker to finish what it holds and then exit."""
        if not self._stop.is_set():
            logger.info("worker_stopping", worker=self.name)
        self._stop.set()

    def run_one(self) -> WorkerResult:
        """Claim and run a single job, if there is one."""
        job = self.store.claim(self.name)
        if job is None:
            return WorkerResult(idle=True)
        return WorkerResult(finished=(self._execute(job),))

    def run_forever(self, *, max_jobs: int = 0, idle_sleep: float = IDLE_SLEEP_SECONDS) -> int:
        """Drain the queue until stopped, returning how many jobs were run.

        ``max_jobs`` bounds the loop, which is what makes it testable and what
        lets an operator run a worker that exits after a batch.
        """
        completed = 0
        while not self._stop.is_set() and (not max_jobs or completed < max_jobs):
            result = self.run_one()
            if result.idle:
                # Waiting on the stop event rather than sleeping, so a signal
                # that arrives during an idle pass is acted on immediately.
                self._stop.wait(idle_sleep)
                continue
            completed += len(result.finished)
        logger.info("worker_stopped", worker=self.name, completed=completed)
        return completed

    # ------------------------------------------------------------ internals ---

    def _execute(self, job: Job) -> Job:
        handler = self.handlers.get(job.kind)
        if handler is None:
            logger.warning("job_kind_unknown", job=job.id, kind=job.kind)
            return self.store.fail(job.id, f"no handler for job kind {job.kind!r}")

        # Every write below names this worker, so the store refuses it if the
        # lease expired while the handler ran. The heartbeat is an optimisation
        # on top of that -- it notices sooner -- not the guarantee.
        with self._heartbeat(job):
            try:
                run_id = handler(job)
            except RewireError as exc:
                return self._finish(job, error=str(exc))
            except Exception as exc:
                logger.exception("job_handler_crashed", job=job.id, kind=job.kind)
                return self._finish(job, error=f"{type(exc).__name__}: {exc}")
        return self._finish(job, run_id=run_id)

    def _finish(self, job: Job, *, run_id: str = "", error: str = "") -> Job:
        """Record the outcome, unless this worker no longer owns the job."""
        try:
            if error:
                return self.store.fail(job.id, error, worker=self.name)
            return self.store.succeed(job.id, run_id=run_id, worker=self.name)
        except LeaseLostError:
            return self._abandon(job)

    def _abandon(self, job: Job) -> Job:
        """Drop a result whose lease expired while the work was running.

        Deliberately writes nothing about the outcome: another worker may already
        own this job, and recording a result for it would overwrite theirs.
        """
        logger.warning("job_result_dropped", job=job.id, worker=self.name)
        return self.store.find(job.id) or job

    def _heartbeat(self, job: Job) -> _Heartbeat:
        interval = max(self.store.lease_seconds * HEARTBEAT_FRACTION, 1.0)
        return _Heartbeat(self.store, job.id, self.name, interval)


class _Heartbeat:
    """Extends a claim in the background for as long as the work runs."""

    def __init__(self, store: JobStore, job_id: str, worker: str, interval: float) -> None:
        self._store = store
        self._job_id = job_id
        self._worker = worker
        self._interval = interval
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = False

    def __enter__(self) -> _Heartbeat:
        self._thread = threading.Thread(target=self._beat, name="rewire-heartbeat", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._done.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval)

    def _beat(self) -> None:
        while not self._done.wait(self._interval):
            if not self._store.heartbeat(self._job_id, self._worker):
                self.lost = True
                logger.warning("job_lease_lost", job=self._job_id, worker=self._worker)
                return


def install_signal_handlers(worker: Worker) -> None:
    """Make SIGINT and SIGTERM ask the worker to stop between jobs.

    Interrupting a migration half way leaves a container running and a patch
    unverified, and the queue would redo the job anyway once the lease expired.
    Finishing costs a couple of minutes; the case where that is too long is the
    case the lease already covers.
    """
    for received in (signal.SIGINT, signal.SIGTERM):
        signal.signal(received, lambda _signum, _frame: worker.stop())


def build_worker(
    store: JobStore,
    *,
    runtime: MigrationRuntime,
    policy: MigrationPolicy | None = None,
    name: str = "",
) -> Worker:
    """A worker that knows how to run migrations.

    The default policy cannot write to a working tree. A queued migration is
    unattended by definition, and Phase 11's rule applies: unattended work
    reaches a repository as a pull request, not as an edit nobody watched.
    """
    handlers = {MIGRATE: migrate_handler(runtime, policy or MigrationPolicy.read_only())}
    worker = Worker(store=store, handlers=handlers)
    if name:
        worker.name = name
    return worker


__all__ = [
    "HEARTBEAT_FRACTION",
    "IDLE_SLEEP_SECONDS",
    "MIGRATE",
    "Handler",
    "JobPayloadError",
    "Worker",
    "WorkerResult",
    "build_worker",
    "install_signal_handlers",
    "migrate_handler",
]
