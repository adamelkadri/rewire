"""Durable background work.

A migration takes sixty to a hundred and twenty seconds, which is longer than
any HTTP request should be held open. This package is what lets the answer be
"here is an identifier, ask me again" instead.

:mod:`~rewire.jobs.models` is the vocabulary — five states, three of them
endings. :mod:`~rewire.jobs.store` is the queue itself, and everything difficult
about it is concurrency: claiming without two workers winning, noticing a worker
that died holding a job, and refusing to hand out a job that keeps killing the
worker that takes it.
"""

from rewire.jobs.models import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    Job,
    JobState,
)
from rewire.jobs.store import JobError, JobStore, open_store

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "Job",
    "JobError",
    "JobState",
    "JobStore",
    "open_store",
]
