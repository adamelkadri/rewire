"""What a watch is, what its state is, and what a check concluded.

The two ideas worth stating plainly live here.

**A baseline is not "the last thing we downloaded".** It is *the specification
version this repository's code is believed to target*. That distinction decides
when it may move, and the rule is narrow: the baseline advances only across a
delta that was either proven to contain nothing breaking, or actually written
into the working tree. A verified patch that was not applied does not move it. A
pull request does not move it either — an unmerged pull request is a proposal,
and recording it as the truth would make Rewire's own state a lie about someone
else's repository.

**A check has more than two outcomes.** "Unchanged", "reformatted but
semantically identical", "changed with nothing breaking" and "changed and
nothing has acted on it yet" call for completely different responses, and only
the last one is worth waking a person for. Collapsing them into changed/unchanged
would make the ordinary case — an upstream that publishes constantly and breaks
nothing — indistinguishable from the case that matters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from rewire.changes.models import ChangeReport
from rewire.core.errors import WatchError
from rewire.services.migrate import MigrationOutcome
from rewire.services.publish import PublishOutcome


class WatchAction(StrEnum):
    """How far a watch is permitted to go when it finds a breaking change.

    Escalating, and each step is opted into explicitly. The default spends no
    money and touches no repository: a monitor that could start calling a model
    the moment it was created would be a monitor nobody could safely leave
    running.
    """

    #: Diff, record and report. No model is called.
    REPORT = "report"
    #: Run the full migration pipeline. Nothing is written to the repository.
    MIGRATE = "migrate"
    #: Migrate, then open a pull request for a verified patch.
    PULL_REQUEST = "pull_request"

    @property
    def calls_a_model(self) -> bool:
        """Whether taking this action costs money."""
        return self is not WatchAction.REPORT


class CheckStatus(StrEnum):
    """What one check of one watch concluded."""

    #: No baseline existed. The specification was adopted as the baseline.
    ADOPTED = "adopted"
    #: The bytes are identical to the baseline, or the server said so.
    UNCHANGED = "unchanged"
    #: The bytes changed but the normalised specification did not.
    REFORMATTED = "reformatted"
    #: The specification changed, and nothing in the delta could break a caller.
    NO_BREAKING_CHANGES = "no_breaking_changes"
    #: Breaking changes found. The watch only reports, so nothing else happened.
    CHANGES_FOUND = "changes_found"
    #: Breaking changes found, and this exact version was already acted on.
    ALREADY_ACTED = "already_acted"
    #: A migration ran for this version. See ``migration`` for what it concluded.
    MIGRATED = "migrated"
    #: The check did not run: disabled, or another check holds the lock.
    SKIPPED = "skipped"
    #: The check could not complete. The baseline was not moved.
    FAILED = "failed"

    @property
    def needs_a_person(self) -> bool:
        """Whether this outcome is waiting on a human decision.

        Drives the exit code, so a cron job can distinguish "nothing to do" from
        "something is waiting for you" without parsing output.
        """
        return self in {
            CheckStatus.CHANGES_FOUND,
            CheckStatus.ALREADY_ACTED,
            CheckStatus.MIGRATED,
        }

    @property
    def is_error(self) -> bool:
        """Whether the check failed rather than concluded."""
        return self is CheckStatus.FAILED


@dataclass(frozen=True, slots=True)
class Watch:
    """A declaration that one specification should be followed for one repository."""

    name: str
    #: A URL or a filesystem path. Interpreted by :mod:`rewire.watch.source`.
    source: str
    repository: Path
    #: Packages the API belongs to, to sharpen impact analysis.
    packages: tuple[str, ...] = ()
    action: WatchAction = WatchAction.REPORT
    #: Branch to propose into. Empty means the repository's default branch.
    base: str = ""
    draft: bool = False
    max_attempts: int = 3
    enabled: bool = True

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable representation."""
        return {
            "name": self.name,
            "source": self.source,
            "repository": str(self.repository),
            "packages": list(self.packages),
            "action": self.action.value,
            "base": self.base,
            "draft": self.draft,
            "max_attempts": self.max_attempts,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> Watch:
        """Rebuild a watch from :meth:`to_dict` output.

        Raises:
            WatchError: The payload is missing a name or a source.
        """
        name = str(payload.get("name") or "")
        source = str(payload.get("source") or "")
        if not name or not source:
            raise WatchError("a watch needs both a name and a source", payload=sorted(payload))
        raw_action = str(payload.get("action") or WatchAction.REPORT.value)
        try:
            action = WatchAction(raw_action)
        except ValueError as exc:
            raise WatchError(f"unknown watch action: {raw_action}", watch=name) from exc
        packages = payload.get("packages")
        attempts = payload.get("max_attempts")
        return cls(
            name=name,
            source=source,
            repository=Path(str(payload.get("repository") or ".")),
            packages=tuple(str(item) for item in packages) if isinstance(packages, list) else (),
            action=action,
            base=str(payload.get("base") or ""),
            draft=bool(payload.get("draft")),
            max_attempts=int(attempts) if isinstance(attempts, int | str) else 3,
            enabled=payload.get("enabled") is not False,
        )


@dataclass(frozen=True, slots=True)
class ActedRecord:
    """What was already done about one version of a specification.

    Recorded for failures as well as successes. A watch on an hourly cron that
    retried a failing migration every hour would spend real money reaching the
    same wrong answer, so a version that has been attempted is not attempted
    again unless a person asks with ``--retry``.
    """

    digest: str
    at: str
    status: str
    run_id: str = ""
    pull_request: str = ""
    detail: str = ""

    def describe(self) -> str:
        """One line naming what happened and where to look."""
        where = self.pull_request or (f"run {self.run_id}" if self.run_id else "")
        return f"{self.status}{f' ({where})' if where else ''} at {self.at}"

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-serialisable representation."""
        return {
            "digest": self.digest,
            "at": self.at,
            "status": self.status,
            "run_id": self.run_id,
            "pull_request": self.pull_request,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> ActedRecord:
        """Rebuild a record from :meth:`to_dict` output."""
        return cls(
            digest=str(payload.get("digest") or ""),
            at=str(payload.get("at") or ""),
            status=str(payload.get("status") or ""),
            run_id=str(payload.get("run_id") or ""),
            pull_request=str(payload.get("pull_request") or ""),
            detail=str(payload.get("detail") or ""),
        )


@dataclass(frozen=True, slots=True)
class WatchState:
    """Everything remembered between checks of one watch.

    ``digest`` is over the bytes and ``semantic_digest`` over the *normalised*
    specification, so a reformatted document is recognised as unchanged without
    a diff. ``etag`` and ``last_modified`` are handed back to the server, which
    turns most checks into a 304 and no download at all.
    """

    #: SHA-256 of the baseline bytes. Empty when no baseline has been adopted.
    digest: str = ""
    #: SHA-256 of the baseline's normalised form.
    semantic_digest: str = ""
    #: The baseline's declared ``info.version``, for display only.
    version: str = ""
    #: Cache validators from the last successful fetch.
    etag: str = ""
    last_modified: str = ""
    last_checked: str = ""
    last_status: str = ""
    #: Versions already acted on, keyed by their byte digest.
    acted: dict[str, ActedRecord] = field(default_factory=dict)

    @property
    def has_baseline(self) -> bool:
        """Whether a baseline specification has been adopted."""
        return bool(self.digest)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable representation."""
        return {
            "digest": self.digest,
            "semantic_digest": self.semantic_digest,
            "version": self.version,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "last_checked": self.last_checked,
            "last_status": self.last_status,
            "acted": {key: record.to_dict() for key, record in self.acted.items()},
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> WatchState:
        """Rebuild state from :meth:`to_dict` output, tolerating missing keys."""
        raw = payload.get("acted")
        acted: dict[str, ActedRecord] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, dict):
                    acted[str(key)] = ActedRecord.from_dict(value)
        return cls(
            digest=str(payload.get("digest") or ""),
            semantic_digest=str(payload.get("semantic_digest") or ""),
            version=str(payload.get("version") or ""),
            etag=str(payload.get("etag") or ""),
            last_modified=str(payload.get("last_modified") or ""),
            last_checked=str(payload.get("last_checked") or ""),
            last_status=str(payload.get("last_status") or ""),
            acted=acted,
        )


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """What one check of one watch established, and what it did about it."""

    watch: Watch
    status: CheckStatus
    #: The specification version now upstream, when one was read.
    version: str = ""
    changes: ChangeReport | None = None
    migration: MigrationOutcome | None = None
    publication: PublishOutcome | None = None
    #: The record that suppressed a repeat, when one did.
    previous: ActedRecord | None = None
    #: Why the check failed or was skipped. Empty otherwise.
    reason: str = ""
    #: Whether the baseline moved as a result of this check.
    baseline_advanced: bool = False

    @property
    def breaking(self) -> int:
        """Breaking and potentially-breaking changes found, if any were counted."""
        if self.changes is None:
            return 0
        return self.changes.summary.breaking + self.changes.summary.potentially_breaking

    def summary_line(self) -> str:
        """One sentence describing the outcome, for a log or a notification."""
        match self.status:
            case CheckStatus.ADOPTED:
                return f"adopted {self.version or 'the current specification'} as the baseline"
            case CheckStatus.UNCHANGED:
                return "the specification has not changed"
            case CheckStatus.REFORMATTED:
                return "the document changed but the specification it describes did not"
            case CheckStatus.NO_BREAKING_CHANGES:
                return "the specification changed, and nothing in the change can break a caller"
            case CheckStatus.CHANGES_FOUND:
                return f"{self.breaking} breaking change(s) found; this watch only reports"
            case CheckStatus.ALREADY_ACTED:
                previous = self.previous.describe() if self.previous else "an earlier check"
                return f"this version was already acted on: {previous}"
            case CheckStatus.MIGRATED:
                migration = self.migration
                return migration.summary_line() if migration else "a migration ran"
            case CheckStatus.SKIPPED:
                return self.reason or "the check did not run"
            case CheckStatus.FAILED:
                return self.reason or "the check could not complete"


__all__ = [
    "ActedRecord",
    "CheckOutcome",
    "CheckStatus",
    "Watch",
    "WatchAction",
    "WatchState",
]
