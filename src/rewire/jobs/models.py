"""What a queued migration is, and the states it can be in.

A migration takes sixty to a hundred and twenty seconds. No HTTP request can
hold one open, so the API has to hand back an identifier and let the caller ask
again — which means the work needs a durable identity of its own, separate from
the run artefacts it will eventually produce.

The state machine is small and deliberately has no "unknown". Every job is
queued, running, or finished one of three ways, and the three endings are
distinct because they call for different responses: a failure may be worth
retrying, a cancellation never is, and a success has a run identifier to read
results from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

#: How long a claimed job stays claimed without a heartbeat. Long enough that an
#: ordinary migration never loses its lease, short enough that a worker killed
#: mid-run is noticed within a few minutes rather than at the next deploy.
DEFAULT_LEASE_SECONDS: Final[int] = 300

#: How many times a job may be handed out before it is treated as poison. A job
#: that kills its worker will be reclaimed every time the lease expires; without
#: a ceiling it does that forever, taking a worker down with it each round.
DEFAULT_MAX_ATTEMPTS: Final[int] = 3


class JobState(StrEnum):
    """Where a job is."""

    #: Waiting for a worker.
    QUEUED = "queued"
    #: Claimed by a worker, with a lease that has not expired.
    RUNNING = "running"
    #: Finished. ``run_id`` names the artefacts.
    SUCCEEDED = "succeeded"
    #: Finished badly, and out of attempts. ``error`` says why.
    FAILED = "failed"
    #: Withdrawn before it finished. Never retried.
    CANCELLED = "cancelled"

    @property
    def is_finished(self) -> bool:
        """Whether nothing further will happen to a job in this state."""
        return self in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}

    @property
    def is_claimable(self) -> bool:
        """Whether a worker may take a job in this state."""
        return self is JobState.QUEUED


def utc_now() -> datetime:
    """Return the current time, timezone-aware."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Job:
    """One unit of queued work.

    ``payload`` is deliberately an opaque mapping rather than a typed request:
    the queue's correctness — claiming, leasing, retrying — has nothing to do
    with what the work *is*, and a queue that had to be changed to carry a new
    kind of job would be a queue coupled to every caller.
    """

    id: str
    kind: str
    state: JobState = JobState.QUEUED
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: Times this job has been handed to a worker, including the current one.
    attempts: int = 0
    #: Worker holding the lease, empty when nobody does.
    worker: str = ""
    #: When the current claim goes stale. ``None`` when not running.
    lease_expires_at: datetime | None = None
    #: Identifier of the run this job produced, once it succeeded.
    run_id: str = ""
    #: Why it failed. Empty otherwise.
    error: str = ""

    @property
    def is_finished(self) -> bool:
        """Whether nothing further will happen to this job."""
        return self.state.is_finished

    def lease_expired(self, *, now: datetime | None = None) -> bool:
        """Whether this job's claim has gone stale.

        A running job whose worker stopped heartbeating is indistinguishable
        from one whose worker is merely slow, which is why the lease is generous
        and the attempt ceiling exists to bound the damage of guessing wrong.
        """
        if self.state is not JobState.RUNNING or self.lease_expires_at is None:
            return False
        return (now or utc_now()) >= self.lease_expires_at

    def describe(self) -> str:
        """One line naming the job and where it got to."""
        match self.state:
            case JobState.QUEUED:
                return f"{self.id} queued"
            case JobState.RUNNING:
                return f"{self.id} running on {self.worker or 'a worker'}"
            case JobState.SUCCEEDED:
                return f"{self.id} succeeded as run {self.run_id}"
            case JobState.FAILED:
                return f"{self.id} failed after {self.attempts} attempt(s): {self.error}"
            case JobState.CANCELLED:
                return f"{self.id} cancelled"


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "Job",
    "JobState",
    "utc_now",
]
