"""The HTTP surface: submit a migration, ask how it went.

This is the most dangerous component in the project, and it is worth being
plain about why. A migration reads a repository, calls a model, and starts a
container that executes code Rewire did not write. Exposing that over HTTP means
exposing all of it, so the two controls here **fail closed**.

**No token, no service.** The application refuses to be constructed without a
shared bearer token. There is no default, no "disabled in development" branch and
no unauthenticated path except liveness, because an API that starts without
credentials is an API somebody will forget to configure.

**No allowlist, no repository.** A request names a path on the server's disk. A
handler that accepted any path would run containers over `/`, or over another
tenant's checkout. Requests are accepted only for paths inside a configured root,
compared after resolution so that `..` and symlinks cannot walk out of one.

Everything long-running happens elsewhere. A request queues a job and returns its
identifier; a worker does the work; the run record answers what happened. No
handler in this module waits for a migration, which is what keeps a request from
holding a connection open for two minutes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from rewire.__version__ import __version__
from rewire.core.config import Settings, get_settings
from rewire.core.errors import ConfigurationError
from rewire.core.logging import get_logger
from rewire.jobs.models import Job, JobState
from rewire.jobs.store import JobError, JobStore
from rewire.jobs.worker import MIGRATE
from rewire.services.record import MigrationRecord, RecordError, read_run

logger = get_logger(__name__)

#: Longest list a single request may ask for.
MAX_PAGE: Final[int] = 200

_bearer = HTTPBearer(auto_error=False)


class MigrationSubmission(BaseModel):
    """What a caller may ask for.

    Mirrors :class:`~rewire.services.migrate.MigrationTask` exactly, and that is
    the point: there is no field here for ``apply`` or ``allow_dirty``, so no
    request body can grant itself permission to write. The policy comes from the
    worker's configuration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: Path
    old_spec: Path
    new_spec: Path
    packages: tuple[str, ...] = ()


class JobView(BaseModel):
    """A job as the API reports it."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    state: JobState
    attempts: int = 0
    created_at: str = ""
    finished_at: str = ""
    run_id: str = ""
    error: str = ""
    #: The run's own record, once there is one to read.
    record: MigrationRecord | None = None

    @classmethod
    def of(cls, job: Job, *, record: MigrationRecord | None = None) -> JobView:
        """Build the view for a job, optionally with its run record."""
        return cls(
            id=job.id,
            kind=job.kind,
            state=job.state,
            attempts=job.attempts,
            created_at=job.created_at.isoformat(),
            finished_at=job.finished_at.isoformat() if job.finished_at else "",
            run_id=job.run_id,
            error=job.error,
            record=record,
        )


class JobList(BaseModel):
    """A page of jobs, with the queue's totals beside it."""

    model_config = ConfigDict(frozen=True)

    jobs: tuple[JobView, ...] = ()
    counts: dict[str, int] = Field(default_factory=dict)


def _require_token(settings: Settings) -> str:
    token = settings.api.token.get_secret_value() if settings.api.token else ""
    if not token:
        raise ConfigurationError(
            "the HTTP API refuses to start without a token",
            remedy="set REWIRE_API__TOKEN to a secret of your choosing",
        )
    return token


def _require_roots(settings: Settings) -> tuple[Path, ...]:
    roots = settings.api.allowed_roots
    if not roots:
        raise ConfigurationError(
            "the HTTP API refuses to start without an allowlist of repository roots",
            remedy="set REWIRE_API__ALLOWED_ROOTS to the directories it may migrate",
        )
    return roots


def resolve_within(candidate: Path, roots: tuple[Path, ...]) -> Path:
    """Return ``candidate`` resolved, if it lies inside one of ``roots``.

    Resolution happens *before* the comparison, so a path containing ``..`` or
    passing through a symlink is judged by where it actually leads rather than by
    how it is spelled.

    Raises:
        HTTPException: The path is outside every allowed root.
    """
    resolved = Path(candidate).expanduser().resolve()
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="that path is not inside an allowed repository root",
    )


def create_app(settings: Settings | None = None, *, store: JobStore | None = None) -> FastAPI:
    """Build the application, refusing if it is not safely configured.

    Raises:
        ConfigurationError: No token, or no allowlist of repository roots.
    """
    resolved = settings or get_settings()
    token = _require_token(resolved)
    roots = _require_roots(resolved)
    resolved.ensure_data_dirs()
    queue = store or JobStore(resolved.jobs_path)

    app = FastAPI(
        title="Rewire",
        version=__version__,
        summary="Queue API migrations and read what happened.",
    )

    def authorise(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        """Reject anything without the configured bearer token."""
        supplied = credentials.credentials if credentials else ""
        # Compared for equality rather than by timing-safe digest because the
        # token is a shared secret on a trusted network, not a password
        # database. Noted rather than glossed over.
        if not supplied or supplied != token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="a valid bearer token is required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    guard = Depends(authorise)

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Liveness. The one route that needs no credential."""
        return {"status": "ok", "version": __version__}

    @app.post(
        "/v1/migrations",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[guard],
    )
    def submit(submission: MigrationSubmission) -> JobView:
        """Queue a migration and return immediately.

        Accepted, not done: a migration takes one to two minutes. Poll the
        returned identifier.
        """
        repository = resolve_within(submission.repository, roots)
        old_spec = resolve_within(submission.old_spec, roots)
        new_spec = resolve_within(submission.new_spec, roots)
        for path in (repository, old_spec, new_spec):
            if not path.exists():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"no such path: {path}",
                )

        job = queue.submit(
            MIGRATE,
            {
                "repository": str(repository),
                "old_spec": str(old_spec),
                "new_spec": str(new_spec),
                "packages": list(submission.packages),
            },
        )
        logger.info("api_migration_queued", job=job.id)
        return JobView.of(job)

    @app.get("/v1/migrations", dependencies=[guard])
    def list_migrations(state: JobState | None = None, limit: int = 50) -> JobList:
        """List jobs oldest first, with the queue's totals."""
        jobs = queue.list_jobs(states=[state] if state else (), limit=min(limit, MAX_PAGE))
        counts = {key.value: value for key, value in queue.counts().items()}
        return JobList(jobs=tuple(JobView.of(job) for job in jobs), counts=counts)

    @app.get("/v1/migrations/{job_id}", dependencies=[guard])
    def get_migration(job_id: str) -> JobView:
        """Return one job, with its run record once it has produced one."""
        job = _find(queue, job_id)
        return JobView.of(job, record=_record_for(resolved, job))

    @app.delete("/v1/migrations/{job_id}", dependencies=[guard])
    def cancel_migration(job_id: str) -> JobView:
        """Withdraw a job that has not finished.

        A running job is marked cancelled rather than interrupted: nothing here
        can reach into the worker's process.
        """
        job = _find(queue, job_id)
        try:
            return JobView.of(queue.cancel(job.id))
        except JobError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.exception_handler(RecordError)
    def _unreadable_record(_request: Request, exc: RecordError) -> None:  # pragma: no cover
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    return app


def _find(queue: JobStore, job_id: str) -> Job:
    job = queue.find(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such job")
    return job


def _record_for(settings: Settings, job: Job) -> MigrationRecord | None:
    """Read the run's record, or ``None`` when there is not one to read.

    A job can name a run whose record was never written — the disk was full, or
    the directory was cleaned. Reporting the job without it beats failing the
    request over an artefact.
    """
    if not job.run_id:
        return None
    try:
        return read_run(settings.runs_dir, job.run_id)
    except RecordError as exc:
        logger.warning("api_record_unreadable", job=job.id, error=str(exc))
        return None


__all__ = [
    "MAX_PAGE",
    "JobList",
    "JobView",
    "MigrationSubmission",
    "create_app",
    "resolve_within",
]
