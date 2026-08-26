"""A durable job queue, on SQLite, correct under concurrent workers.

The queue exists because a migration takes a minute or two and an HTTP request
cannot wait. Everything difficult about it is concurrency, so that is what this
module is shaped around.

**A claim is one guarded UPDATE.** Selecting a candidate and then updating it is
a race: two workers can select the same row. The update carries the state it
expected in its `WHERE` clause, so the loser changes nothing, sees zero rows
affected and tries the next candidate. This is the same statement on Postgres,
which is the point — the correctness does not rest on a SQLite-specific lock.

**A claim expires.** A worker that is killed mid-migration cannot release
anything, and a job stuck in `running` forever is a job nobody will ever redo.
Claims carry a lease which the worker extends while it works; an expired lease
returns the job to the queue.

**A job that keeps killing its worker is stopped.** Reclaiming forever is how
one poison job takes down a worker pool one process at a time, so attempts are
counted and a job out of attempts fails permanently with the reason recorded.

**Writes are serialised, reads are not blocked.** SQLite in WAL mode with a busy
timeout, which is the configuration under which its concurrency is adequate for
one machine. It is not adequate for several, and this module does not pretend
otherwise: see the note on Postgres in ADR-064.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from rewire.core.errors import RewireError
from rewire.core.logging import get_logger
from rewire.jobs.models import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    Job,
    JobState,
    utc_now,
)

logger = get_logger(__name__)

#: Seconds SQLite waits for a write lock before giving up. Generous because the
#: writes here are tiny and the alternative to waiting is a spurious failure.
BUSY_TIMEOUT_MS: Final[int] = 5_000

#: Kept deliberately plain: no SQLite-only types, no autoincrement, timestamps
#: as ISO-8601 text. Every statement in this module runs unmodified on Postgres.
SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    state             TEXT NOT NULL,
    payload           TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    worker            TEXT NOT NULL DEFAULT '',
    lease_expires_at  TEXT,
    run_id            TEXT NOT NULL DEFAULT '',
    error             TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs (state, created_at);
"""

_COLUMNS: Final[str] = (
    "id, kind, state, payload, created_at, started_at, finished_at, "
    "attempts, worker, lease_expires_at, run_id, error"
)


class JobError(RewireError):
    """A job could not be found, or was asked to do something its state forbids."""

    code = "job_error"


class JobStore:
    """The queue. One SQLite file, safe for many workers on one machine."""

    def __init__(
        self,
        path: Path | str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        """Open (and create if needed) the queue at ``path``."""
        self.path = Path(path)
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    # ---------------------------------------------------------- connection ---

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A connection per operation, in WAL mode, committing on success.

        Per operation rather than per store: a connection is cheap, and a shared
        one would have to be guarded against the threads this queue exists to
        support.
        """
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            with connection:
                yield connection
        finally:
            connection.close()

    # -------------------------------------------------------------- submit ---

    def submit(self, kind: str, payload: dict[str, Any] | None = None) -> Job:
        """Add a job to the back of the queue."""
        job = Job(id=uuid.uuid4().hex[:16], kind=kind, payload=dict(payload or {}))
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO jobs ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",  # noqa: S608 - fixed column constant; every value is bound
                _to_row(job),
            )
        logger.info("job_submitted", job=job.id, kind=kind)
        return job

    # --------------------------------------------------------------- claim ---

    def claim(self, worker: str, *, now: datetime | None = None) -> Job | None:
        """Take the oldest claimable job, or return ``None`` if there is none.

        The update names the state it expected, so two workers racing for the
        same row cannot both win: the loser affects zero rows and moves on. That
        is why this is a loop rather than a single statement.
        """
        moment = now or utc_now()
        self.recover_expired(now=moment)
        deadline = moment + timedelta(seconds=self.lease_seconds)

        with self._connect() as connection:
            for _ in range(_CLAIM_ATTEMPTS):
                row = connection.execute(
                    "SELECT id, attempts FROM jobs WHERE state = ? ORDER BY created_at, id LIMIT 1",
                    (JobState.QUEUED.value,),
                ).fetchone()
                if row is None:
                    return None
                updated = connection.execute(
                    "UPDATE jobs SET state = ?, worker = ?, started_at = ?, "
                    "lease_expires_at = ?, attempts = attempts + 1 "
                    "WHERE id = ? AND state = ?",
                    (
                        JobState.RUNNING.value,
                        worker,
                        _stamp(moment),
                        _stamp(deadline),
                        row["id"],
                        JobState.QUEUED.value,
                    ),
                )
                if updated.rowcount == 1:
                    claimed = self._require(connection, str(row["id"]))
                    logger.info("job_claimed", job=claimed.id, worker=worker)
                    return claimed
        return None  # pragma: no cover - only if every candidate is taken repeatedly

    def heartbeat(self, job_id: str, worker: str, *, now: datetime | None = None) -> bool:
        """Extend a claim, returning whether this worker still holds it.

        ``False`` means the lease expired and somebody else may already have the
        job. A worker that gets it should stop, because finishing would write a
        result for work another worker is now redoing.
        """
        moment = now or utc_now()
        deadline = moment + timedelta(seconds=self.lease_seconds)
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE jobs SET lease_expires_at = ? WHERE id = ? AND state = ? AND worker = ?",
                (_stamp(deadline), job_id, JobState.RUNNING.value, worker),
            )
        return updated.rowcount == 1

    def recover_expired(self, *, now: datetime | None = None) -> tuple[Job, ...]:
        """Return jobs whose lease went stale to the queue, or fail them.

        A worker killed mid-migration releases nothing, so this is the only path
        by which its job runs again. Jobs out of attempts are failed here rather
        than requeued: a job that reliably kills its worker would otherwise take
        one down every lease period.
        """
        moment = now or utc_now()
        recovered: list[Job] = []
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM jobs WHERE state = ? AND lease_expires_at <= ?",  # noqa: S608 - as above
                (JobState.RUNNING.value, _stamp(moment)),
            ).fetchall()
            for row in rows:
                job = _from_row(row)
                if job.attempts >= self.max_attempts:
                    connection.execute(
                        "UPDATE jobs SET state = ?, finished_at = ?, worker = '', "
                        "lease_expires_at = NULL, error = ? WHERE id = ? AND state = ?",
                        (
                            JobState.FAILED.value,
                            _stamp(moment),
                            f"abandoned by its worker {job.attempts} time(s)",
                            job.id,
                            JobState.RUNNING.value,
                        ),
                    )
                    logger.warning("job_abandoned", job=job.id, attempts=job.attempts)
                else:
                    connection.execute(
                        "UPDATE jobs SET state = ?, worker = '', started_at = NULL, "
                        "lease_expires_at = NULL WHERE id = ? AND state = ?",
                        (JobState.QUEUED.value, job.id, JobState.RUNNING.value),
                    )
                    logger.info("job_requeued", job=job.id, attempts=job.attempts)
                recovered.append(self._require(connection, job.id))
        return tuple(recovered)

    # ------------------------------------------------------------- finish ---

    def succeed(self, job_id: str, *, run_id: str = "", now: datetime | None = None) -> Job:
        """Record that a job finished, naming the run it produced.

        Raises:
            JobError: The job does not exist, or was not running.
        """
        return self._finish(job_id, JobState.SUCCEEDED, run_id=run_id, now=now)

    def fail(self, job_id: str, error: str, *, now: datetime | None = None) -> Job:
        """Record that a job failed and will not be retried by this path.

        A worker that catches an exception knows something a lease expiry does
        not: the job ran to a conclusion and that conclusion was bad. Retrying
        that is a decision for a person, so it is not made here.

        Raises:
            JobError: The job does not exist, or was not running.
        """
        return self._finish(job_id, JobState.FAILED, error=error, now=now)

    def cancel(self, job_id: str, *, now: datetime | None = None) -> Job:
        """Withdraw a job that has not finished.

        A running job is cancelled where it stands rather than interrupted:
        nothing here can reach into another process, and claiming otherwise
        would be worse than saying so.

        Raises:
            JobError: The job does not exist, or has already finished.
        """
        moment = now or utc_now()
        with self._connect() as connection:
            existing = self._require(connection, job_id)
            if existing.is_finished:
                raise JobError(
                    f"cannot cancel a job that is already {existing.state.value}", job=job_id
                )
            connection.execute(
                "UPDATE jobs SET state = ?, finished_at = ?, worker = '', "
                "lease_expires_at = NULL WHERE id = ?",
                (JobState.CANCELLED.value, _stamp(moment), job_id),
            )
            logger.info("job_cancelled", job=job_id)
            return self._require(connection, job_id)

    def _finish(
        self,
        job_id: str,
        state: JobState,
        *,
        run_id: str = "",
        error: str = "",
        now: datetime | None = None,
    ) -> Job:
        moment = now or utc_now()
        with self._connect() as connection:
            existing = self._require(connection, job_id)
            if existing.state is not JobState.RUNNING:
                raise JobError(
                    f"cannot finish a job that is {existing.state.value}, not running",
                    job=job_id,
                )
            connection.execute(
                "UPDATE jobs SET state = ?, finished_at = ?, run_id = ?, error = ?, "
                "worker = '', lease_expires_at = NULL WHERE id = ? AND state = ?",
                (
                    state.value,
                    _stamp(moment),
                    run_id,
                    error,
                    job_id,
                    JobState.RUNNING.value,
                ),
            )
            finished = self._require(connection, job_id)
        logger.info("job_finished", job=job_id, state=state.value, run_id=run_id)
        return finished

    # --------------------------------------------------------------- reads ---

    def get(self, job_id: str) -> Job:
        """Return one job.

        Raises:
            JobError: No job with that identifier.
        """
        with self._connect() as connection:
            return self._require(connection, job_id)

    def find(self, job_id: str) -> Job | None:
        """Return one job, or ``None`` if there is no such job."""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM jobs WHERE id = ?",  # noqa: S608 - as above
                (job_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def list_jobs(
        self, *, states: Sequence[JobState] = (), kind: str = "", limit: int = 100
    ) -> tuple[Job, ...]:
        """Return jobs oldest first, optionally filtered."""
        clauses: list[str] = []
        values: list[Any] = []
        if states:
            clauses.append(f"state IN ({','.join('?' * len(states))})")
            values.extend(state.value for state in states)
        if kind:
            clauses.append("kind = ?")
            values.append(kind)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                # `where` is built from placeholders and fixed column names only;
                # every filter value is bound below.
                f"SELECT {_COLUMNS} FROM jobs{where} ORDER BY created_at, id LIMIT ?",  # noqa: S608
                (*values, limit),
            ).fetchall()
        return tuple(_from_row(row) for row in rows)

    def counts(self) -> dict[JobState, int]:
        """How many jobs are in each state, including the empty ones."""
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")
            found = {str(row["state"]): int(row["n"]) for row in rows}
        return {state: found.get(state.value, 0) for state in JobState}

    def _require(self, connection: sqlite3.Connection, job_id: str) -> Job:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM jobs WHERE id = ?",  # noqa: S608 - as above
            (job_id,),
        ).fetchone()
        if row is None:
            raise JobError("no job with that identifier", job=job_id)
        return _from_row(row)


#: How many candidates a claim will try before giving up this pass. Bounded so a
#: pathologically contended queue returns rather than spinning.
_CLAIM_ATTEMPTS: Final[int] = 8


def _stamp(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _to_row(job: Job) -> tuple[Any, ...]:
    return (
        job.id,
        job.kind,
        job.state.value,
        json.dumps(job.payload, sort_keys=True),
        _stamp(job.created_at),
        _stamp(job.started_at),
        _stamp(job.finished_at),
        job.attempts,
        job.worker,
        _stamp(job.lease_expires_at),
        job.run_id,
        job.error,
    )


def _from_row(row: sqlite3.Row) -> Job:
    payload = json.loads(row["payload"])
    return Job(
        id=str(row["id"]),
        kind=str(row["kind"]),
        state=JobState(row["state"]),
        payload=payload if isinstance(payload, dict) else {},
        created_at=_parse(row["created_at"]) or utc_now(),
        started_at=_parse(row["started_at"]),
        finished_at=_parse(row["finished_at"]),
        attempts=int(row["attempts"]),
        worker=str(row["worker"]),
        lease_expires_at=_parse(row["lease_expires_at"]),
        run_id=str(row["run_id"]),
        error=str(row["error"]),
    )


def open_store(path: Path | str, **kwargs: Any) -> JobStore:
    """Open the queue, creating it if it does not exist."""
    return JobStore(path, **kwargs)


__all__ = [
    "BUSY_TIMEOUT_MS",
    "SCHEMA",
    "JobError",
    "JobStore",
    "open_store",
]
