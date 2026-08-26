"""What a migration run concluded, and its machine-readable record.

Every run writes one of these beside its traces, including the runs where
nothing happened — a dataset of only the interesting runs is a dataset with a
hole in it.

Until now it was a dictionary assembled by hand at the point of writing, which
is fine while nothing reads it back. Phase 13 changes that: an HTTP client asking
"what happened in run X" is answered from this file, and at that moment the
format stops being an implementation detail and becomes an interface. Three
things follow.

**It is a model, not a dictionary.** The fields are declared once, validated on
the way in and on the way out, and a typo in a key name is a failure at the point
it happens rather than a `None` three layers away.

**It carries a version.** An integer, bumped only when a field is removed or its
meaning changes — adding a field is not a breaking change and does not bump it.
Without one, the first format change makes every previously written record
indistinguishable from a current one.

**Reading a version it does not understand is refused, not guessed.** A reader
that shrugs and carries on is how a benchmark quietly computes a number from
fields that no longer mean what it thinks. Writing stays best-effort in the
opposite direction: a record that cannot be written is logged and the run still
returns its answer, because the artefact is evidence about the work and not the
work itself.

:class:`MigrationStatus` lives here rather than beside the pipeline for one
reason: it is the vocabulary of the *outcome*, which the record is the durable
form of. Putting it here lets the pipeline depend on the record instead of the
other way round, so nothing has to import a module that is about to import it
back.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from rewire.core.errors import RewireError
from rewire.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from rewire.sandbox.models import Verdict
    from rewire.services.migrate import MigrationOutcome

logger = get_logger(__name__)


class MigrationStatus(StrEnum):
    """What a migration run concluded.

    Six outcomes rather than a boolean, because they call for different actions
    and three of them are not failures.
    """

    #: The two specifications differ in nothing that could break a caller.
    NO_BREAKING_CHANGES = "no_breaking_changes"
    #: There are breaking changes, but nothing in this repository uses them.
    NO_AFFECTED_CODE = "no_affected_code"
    #: A patch was verified and written to the working tree.
    APPLIED = "applied"
    #: A patch was verified. Nothing was written, because nothing asked it to be.
    VERIFIED = "verified"
    #: A patch was verified, but writing it was refused. See ``refusal``.
    REFUSED = "refused"
    #: A patch exists but the sandbox did not confirm it.
    UNVERIFIED = "unverified"
    #: The agent produced no patch at all.
    NO_PATCH = "no_patch"

    @property
    def is_success(self) -> bool:
        """Whether this outcome means the run did its job.

        "Nothing here is affected" is a success. Most runs will say it once
        Phase 12 watches upstream specifications, and treating it as a failure
        would make a healthy repository look broken every time an API moves.
        """
        return self in {
            MigrationStatus.NO_BREAKING_CHANGES,
            MigrationStatus.NO_AFFECTED_CODE,
            MigrationStatus.APPLIED,
            MigrationStatus.VERIFIED,
        }


#: Bumped when a field is removed or its meaning changes. Adding a field is not
#: a breaking change: a reader written against version 1 keeps working, because
#: it asks for the fields it knows and ignores the rest.
RECORD_VERSION: Final[int] = 1

#: Name of the file inside a run's directory.
RECORD_FILENAME: Final[str] = "migration.json"


class RecordError(RewireError):
    """A run record could not be read, or is a version this build cannot read."""

    code = "record_error"


class AttemptRecord(BaseModel):
    """What one attempt of the repair loop did."""

    model_config = ConfigDict(frozen=True)

    number: int
    #: The sandbox's verdict, or ``None`` if the attempt never reached it.
    verdict: str | None = None
    files: int = 0
    tokens: int = 0


class MigrationRecord(BaseModel):
    """Everything a run is willing to say about itself afterwards.

    Deliberately flat and free of the objects it was derived from: this is read
    by a benchmark, an HTTP handler and a person with ``cat``, none of which
    should have to import Rewire's models to make sense of it.
    """

    model_config = ConfigDict(frozen=True)

    version: int = RECORD_VERSION
    run_id: str
    status: MigrationStatus
    #: One sentence describing the outcome.
    summary: str = ""
    duration_seconds: float = 0.0
    files_written: tuple[str, ...] = ()
    #: Why writing was refused, when it was.
    refusal: str = ""
    #: The specification diff's own summary counts, or ``None`` if none was run.
    changes: dict[str, Any] | None = None
    affected_locations: int = 0
    attempts: tuple[AttemptRecord, ...] = ()
    repaired: bool = False
    total_tokens: int = 0
    total_cost_usd: float | None = None

    @property
    def succeeded(self) -> bool:
        """Whether the run did its job, by the same rule the status enum uses."""
        return self.status.is_success

    @classmethod
    def of(cls, outcome: MigrationOutcome) -> MigrationRecord:
        """Build the record for a finished run."""
        repair = outcome.repair
        return cls(
            run_id=outcome.run_id,
            status=outcome.status,
            summary=outcome.summary_line(),
            duration_seconds=outcome.duration_seconds,
            files_written=tuple(outcome.written),
            refusal=outcome.refusal,
            changes=outcome.changes.summary.model_dump(mode="json") if outcome.changes else None,
            affected_locations=outcome.impact.summary.locations if outcome.impact else 0,
            attempts=tuple(
                AttemptRecord(
                    number=attempt.number,
                    verdict=_verdict_of(attempt.verdict),
                    files=len(attempt.patch.files),
                    tokens=attempt.result.summary.usage.total_tokens,
                )
                for attempt in (repair.attempts if repair else ())
            ),
            repaired=repair.repaired if repair else False,
            total_tokens=repair.total_tokens if repair else 0,
            total_cost_usd=repair.total_cost_usd if repair else None,
        )

    def to_json(self) -> str:
        """Render the record as it is stored."""
        return json.dumps(self.model_dump(mode="json"), indent=2) + "\n"


def _verdict_of(verdict: Verdict | None) -> str | None:
    return verdict.value if verdict is not None else None


def record_path(runs_dir: Path, run_id: str) -> Path:
    """Where the record for ``run_id`` lives."""
    return Path(runs_dir) / run_id / RECORD_FILENAME


def write_record(runs_dir: Path, outcome: MigrationOutcome) -> Path | None:
    """Write the record for a finished run, returning where it went.

    Best-effort by design: a record that cannot be written is logged and
    ``None`` is returned, because the run has already done its work and refusing
    to report it would lose more than the file does.
    """
    path = record_path(runs_dir, outcome.run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(MigrationRecord.of(outcome).to_json(), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - the run directory is already writable
        logger.warning("migration_record_not_written", run_id=outcome.run_id, error=str(exc))
        return None
    return path


def read_record(path: Path | str) -> MigrationRecord:
    """Read one record, refusing a version this build does not understand.

    Raises:
        RecordError: The file is missing, unparseable, invalid, or was written by
            a newer Rewire whose fields may not mean what this one assumes.
    """
    file_path = Path(path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecordError(f"the run record could not be read: {exc}", path=str(file_path)) from exc
    if not isinstance(payload, dict):
        raise RecordError("a run record must be an object", path=str(file_path))

    version = payload.get("version")
    if not isinstance(version, int) or version > RECORD_VERSION:
        raise RecordError(
            "this run record was written by a newer version of Rewire",
            path=str(file_path),
            found=version,
            understood=RECORD_VERSION,
        )

    try:
        return MigrationRecord.model_validate(payload)
    except ValidationError as exc:
        raise RecordError(
            f"the run record is not valid: {exc.error_count()} problem(s)", path=str(file_path)
        ) from exc


def read_run(runs_dir: Path, run_id: str) -> MigrationRecord:
    """Read the record for one run by identifier.

    Raises:
        RecordError: No such run, or its record cannot be read.
    """
    return read_record(record_path(runs_dir, run_id))


def read_all(runs_dir: Path) -> tuple[MigrationRecord, ...]:
    """Read every readable record under ``runs_dir``, newest identifier last.

    Unreadable records are logged and skipped rather than raised: one corrupt
    file must not make the whole history unreadable. Callers that need to know
    something was skipped have the log; callers that need one specific record
    should ask for it by name and get the error.
    """
    records: list[MigrationRecord] = []
    for path in sorted(Path(runs_dir).glob(f"*/{RECORD_FILENAME}")):
        try:
            records.append(read_record(path))
        except RecordError as exc:
            logger.warning("migration_record_skipped", path=str(path), error=str(exc))
    return tuple(records)


__all__ = [
    "RECORD_FILENAME",
    "RECORD_VERSION",
    "AttemptRecord",
    "MigrationRecord",
    "MigrationStatus",
    "RecordError",
    "read_all",
    "read_record",
    "read_run",
    "record_path",
    "write_record",
]
