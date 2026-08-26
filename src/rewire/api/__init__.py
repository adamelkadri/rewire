"""HTTP API surface (FastAPI). Introduced in Phase 13.

Deliberately thin. Every handler either queues work or reads a record; nothing
here runs a migration, because a migration takes minutes and a request should
not. The interesting decisions are the two refusals in
:func:`~rewire.api.app.create_app` — no token, no service; no allowlist, no
repository.
"""

from rewire.api.app import (
    JobList,
    JobView,
    MigrationSubmission,
    create_app,
    resolve_within,
)

__all__ = [
    "JobList",
    "JobView",
    "MigrationSubmission",
    "create_app",
    "resolve_within",
]
