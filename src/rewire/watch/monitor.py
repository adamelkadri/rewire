"""One check of one watch: fetch, compare, decide, and record what was decided.

The whole phase is here, and it is mostly a set of refusals to do work.

**Three cheap questions come before the expensive one.** A conditional request
usually returns 304 and no body. A byte digest settles it when it does not. A
digest of the *normalised* specification settles the case where a document was
regenerated with different key order or whitespace. Only what survives all three
is diffed, and only a diff containing something that can break a caller is
allowed to reach a model.

**A version is acted on once.** Every attempt is recorded against the digest that
provoked it, successes and failures alike. Without that, a watch on an hourly
cron whose migration fails would spend real money every hour reaching the same
wrong answer, and one whose migration succeeds would open a pull request every
hour for a change that already has one. ``--retry`` is how a person asks again.

**The baseline moves only when it is true.** It advances across a delta proven to
contain nothing breaking, and otherwise not at all — not when a patch verified,
and not when a pull request opened, because an unmerged pull request is a
proposal about someone else's repository, not a fact about it. ``rewire watch
accept`` is how a person says the code has caught up.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from rewire.changes.differ import diff_specs
from rewire.changes.models import ChangeReport
from rewire.changes.spec import ApiSpec, load_spec, parse_spec_text
from rewire.core.errors import RewireError, WatchError
from rewire.core.logging import get_logger
from rewire.services.migrate import MigrationOutcome, MigrationRequest
from rewire.services.publish import PublishOutcome, PublishRequest, check_publishable
from rewire.watch.models import (
    ActedRecord,
    CheckOutcome,
    CheckStatus,
    Watch,
    WatchAction,
    WatchState,
)
from rewire.watch.source import Fetched, fetch
from rewire.watch.store import WatchStore

logger = get_logger(__name__)

#: Default ceiling on a fetched specification, matching the loader's own.
MAX_SPEC_BYTES: Final[int] = 32 * 1024 * 1024

#: A check could not complete.
EXIT_CHECK_FAILED: Final[int] = 1

#: A check completed and found something a person has to decide about.
EXIT_ACTION_REQUIRED: Final[int] = 2

# The injection points. Every one of them spends money, touches a network or
# reads a clock, which is exactly the set of things a test of this logic has to
# be able to replace.
MigrateCallable = Callable[[MigrationRequest], MigrationOutcome]
PublishCallable = Callable[[MigrationOutcome, PublishRequest], PublishOutcome]
FetchCallable = Callable[..., Fetched]
ClockCallable = Callable[[], str]


@dataclass(frozen=True, slots=True)
class CheckPolicy:
    """How one pass over the watches is allowed to behave."""

    timeout_seconds: float = 30.0
    max_bytes: int = MAX_SPEC_BYTES
    #: Permit plain HTTP, before and after redirects.
    allow_http: bool = False
    #: Act again on a version that has already been acted on.
    retry: bool = False
    #: With ``pull_request``: branch and commit, but never push.
    dry_run: bool = False
    #: Leading segment of any branch a watch creates.
    branch_prefix: str = "rewire"


def utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def semantic_digest(spec: ApiSpec) -> str:
    """Return a digest of what a specification *means*, not how it is written.

    Taken over the normalised model, so key order, indentation, comments and the
    JSON/YAML choice all fall out. This is what makes "the vendor regenerated
    their document" distinguishable from "the vendor changed their API".
    """
    payload = json.dumps(spec.model_dump(mode="json", exclude_none=True), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Check:
    """One check in progress. Holds what the decision needs, and nothing else."""

    watch: Watch
    store: WatchStore
    policy: CheckPolicy
    migrate: MigrateCallable | None
    publish: PublishCallable | None
    fetcher: FetchCallable
    clock: ClockCallable

    # ------------------------------------------------------------- helpers ---

    def conclude(
        self,
        status: CheckStatus,
        *,
        state: WatchState,
        version: str = "",
        changes: ChangeReport | None = None,
        migration: MigrationOutcome | None = None,
        publication: PublishOutcome | None = None,
        previous: ActedRecord | None = None,
        reason: str = "",
        baseline_advanced: bool = False,
    ) -> CheckOutcome:
        """Persist the new state and return the outcome describing it."""
        self.store.write_state(
            self.watch.name,
            replace(state, last_checked=self.clock(), last_status=status.value),
        )
        outcome = CheckOutcome(
            watch=self.watch,
            status=status,
            version=version,
            changes=changes,
            migration=migration,
            publication=publication,
            previous=previous,
            reason=reason,
            baseline_advanced=baseline_advanced,
        )
        logger.info(
            "watch_checked",
            watch=self.watch.name,
            status=status.value,
            breaking=outcome.breaking,
            baseline_advanced=baseline_advanced,
        )
        return outcome

    # ----------------------------------------------------------------- run ---

    def run(self) -> CheckOutcome:
        """Do the check, from the cheapest question to the most expensive."""
        state = self.store.read_state(self.watch.name)

        try:
            fetched = self.fetcher(
                self.watch.source,
                etag=state.etag,
                last_modified=state.last_modified,
                timeout_seconds=self.policy.timeout_seconds,
                max_bytes=self.policy.max_bytes,
                allow_http=self.policy.allow_http,
            )
        except WatchError as exc:
            return self.conclude(CheckStatus.FAILED, state=state, reason=str(exc))

        validators = replace(state, etag=fetched.etag, last_modified=fetched.last_modified)
        if fetched.not_modified or (state.has_baseline and fetched.digest == state.digest):
            return self.conclude(CheckStatus.UNCHANGED, state=validators, version=state.version)

        try:
            candidate = parse_spec_text(fetched.text, source=self.watch.source)
        except RewireError as exc:
            # The validators are deliberately not stored: an unparseable body
            # must not teach the next check to accept a 304 in its place.
            return self.conclude(CheckStatus.FAILED, state=state, reason=str(exc))

        version = candidate.metadata.version or ""
        meaning = semantic_digest(candidate)

        if not state.has_baseline:
            self.store.write_baseline(self.watch.name, fetched.text, suffix=fetched.suffix)
            return self.conclude(
                CheckStatus.ADOPTED,
                state=replace(
                    validators,
                    digest=fetched.digest,
                    semantic_digest=meaning,
                    version=version,
                ),
                version=version,
                baseline_advanced=True,
            )

        if meaning == state.semantic_digest:
            self.store.write_baseline(self.watch.name, fetched.text, suffix=fetched.suffix)
            return self.conclude(
                CheckStatus.REFORMATTED,
                state=replace(validators, digest=fetched.digest, version=version),
                version=version,
                baseline_advanced=True,
            )

        baseline_path = self.store.baseline_path(self.watch.name)
        candidate_path = self.store.write_candidate(
            self.watch.name, fetched.text, suffix=fetched.suffix
        )
        try:
            changes = diff_specs(load_spec(baseline_path), candidate)
        except RewireError as exc:
            # The baseline parsed when it was adopted, so reaching this means
            # someone edited the stored copy. Reported rather than raised, so
            # one broken watch does not stop the rest of the pass.
            return self.conclude(CheckStatus.FAILED, state=state, reason=str(exc))

        if changes.summary.breaking + changes.summary.potentially_breaking == 0:
            self.store.write_baseline(self.watch.name, fetched.text, suffix=fetched.suffix)
            return self.conclude(
                CheckStatus.NO_BREAKING_CHANGES,
                state=replace(
                    validators,
                    digest=fetched.digest,
                    semantic_digest=meaning,
                    version=version,
                ),
                version=version,
                changes=changes,
                baseline_advanced=True,
            )

        previous = state.acted.get(fetched.digest)
        if previous is not None and not self.policy.retry:
            return self.conclude(
                CheckStatus.ALREADY_ACTED,
                state=validators,
                version=version,
                changes=changes,
                previous=previous,
            )

        if self.watch.action is WatchAction.REPORT:
            # Deliberately not recorded as acted. Reporting is not acting, the
            # finding is still true on the next check, and `rewire watch accept`
            # is how a person makes it stop.
            return self.conclude(
                CheckStatus.CHANGES_FOUND,
                state=validators,
                version=version,
                changes=changes,
            )

        return self.act(
            state=validators,
            digest=fetched.digest,
            version=version,
            changes=changes,
            baseline_path=baseline_path,
            candidate_path=candidate_path,
        )

    # ----------------------------------------------------------------- act ---

    def act(
        self,
        *,
        state: WatchState,
        digest: str,
        version: str,
        changes: ChangeReport,
        baseline_path: Path,
        candidate_path: Path,
    ) -> CheckOutcome:
        """Run the migration this watch asked for, and record that it ran."""
        wants_pull_request = self.watch.action is WatchAction.PULL_REQUEST
        migrate = self.migrate
        publish = self.publish

        if migrate is None or (wants_pull_request and publish is None):
            return self.conclude(
                CheckStatus.FAILED,
                state=state,
                version=version,
                changes=changes,
                reason=(
                    f"this watch is configured to {self.watch.action.value}, "
                    "but this process has no way to do it"
                ),
            )

        # Asked before the model, not after. Every reason publishing can fail is
        # knowable in milliseconds, and an unattended monitor that discovers one
        # after an agent run and two container runs has spent real money to
        # learn something that was free.
        if wants_pull_request and (refusal := check_publishable(self.watch.repository)):
            return self.conclude(
                CheckStatus.FAILED,
                state=state,
                version=version,
                changes=changes,
                reason=f"cannot open a pull request: {refusal}",
            )

        request = MigrationRequest(
            repository=self.watch.repository,
            old_spec=baseline_path,
            new_spec=candidate_path,
            packages=self.watch.packages,
            apply=False,
            max_attempts=self.watch.max_attempts,
        )
        try:
            outcome = migrate(request)
        except RewireError as exc:
            return self.conclude(
                CheckStatus.FAILED,
                state=_record(state, digest, self.clock(), status="failed", detail=str(exc)),
                version=version,
                changes=changes,
                reason=str(exc),
            )

        publication: PublishOutcome | None = None
        if wants_pull_request and publish is not None:
            publication = publish(
                outcome,
                PublishRequest(
                    repository=self.watch.repository,
                    prefix=self.policy.branch_prefix,
                    draft=self.watch.draft,
                    base=self.watch.base,
                    dry_run=self.policy.dry_run,
                ),
            )

        pull_request = publication.pull_request if publication is not None else None
        return self.conclude(
            CheckStatus.MIGRATED,
            state=_record(
                state,
                digest,
                self.clock(),
                status=publication.status.value if publication else outcome.status.value,
                run_id=outcome.run_id,
                pull_request=pull_request.url if pull_request is not None else "",
                detail=publication.refusal if publication else outcome.refusal,
            ),
            version=version,
            changes=changes,
            migration=outcome,
            publication=publication,
        )


def _record(
    state: WatchState,
    digest: str,
    at: str,
    *,
    status: str,
    run_id: str = "",
    pull_request: str = "",
    detail: str = "",
) -> WatchState:
    """Return ``state`` with this version marked as acted on."""
    record = ActedRecord(
        digest=digest,
        at=at,
        status=status,
        run_id=run_id,
        pull_request=pull_request,
        detail=detail,
    )
    return replace(state, acted={**state.acted, digest: record})


def check_watch(
    watch: Watch,
    *,
    store: WatchStore,
    policy: CheckPolicy | None = None,
    migrate: MigrateCallable | None = None,
    publish: PublishCallable | None = None,
    fetcher: FetchCallable = fetch,
    clock: ClockCallable = utc_now,
) -> CheckOutcome:
    """Check one watch and return what it concluded.

    Ordinary failures do not raise: a fetch that timed out or a document that
    will not parse comes back as :attr:`CheckStatus.FAILED`, because one
    unreachable vendor must not stop the other watches from running.

    Args:
        watch: The declaration to check.
        store: Where its baseline and state live.
        policy: Network limits and retry behaviour.
        migrate: Runs the migration pipeline. Required for a watch that acts.
        publish: Opens the pull request. Required for a ``pull_request`` watch.
        fetcher: Reads the specification. Replaced in tests.
        clock: Supplies timestamps. Replaced in tests.
    """
    if not watch.enabled:
        return CheckOutcome(watch=watch, status=CheckStatus.SKIPPED, reason="the watch is disabled")

    with store.lock(watch.name) as held:
        if not held:
            return CheckOutcome(
                watch=watch,
                status=CheckStatus.SKIPPED,
                reason="another check of this watch is already running",
            )
        return _Check(
            watch=watch,
            store=store,
            policy=policy or CheckPolicy(),
            migrate=migrate,
            publish=publish,
            fetcher=fetcher,
            clock=clock,
        ).run()


def check_all(
    watches: Iterable[Watch],
    *,
    store: WatchStore,
    policy: CheckPolicy | None = None,
    migrate: MigrateCallable | None = None,
    publish: PublishCallable | None = None,
    fetcher: FetchCallable = fetch,
    clock: ClockCallable = utc_now,
) -> tuple[CheckOutcome, ...]:
    """Check every watch in order, and return one outcome for each."""
    return tuple(
        check_watch(
            watch,
            store=store,
            policy=policy,
            migrate=migrate,
            publish=publish,
            fetcher=fetcher,
            clock=clock,
        )
        for watch in watches
    )


def accept(watch: Watch, *, store: WatchStore, clock: ClockCallable = utc_now) -> str:
    """Advance the baseline to the specification the last check reported.

    The manual half of the baseline rule: a person merged the pull request, or
    migrated by hand, or decided the change does not matter, and is telling
    Rewire that the code now targets the newer specification. It reads the
    candidate the last check stored rather than fetching a fresh copy, so what
    is accepted is exactly what was reported.

    Returns:
        The version now recorded as the baseline.

    Raises:
        WatchError: No newer specification has been stored.
        SpecParseError: The stored candidate cannot be parsed.
    """
    candidates = sorted(store.directory(watch.name).glob("candidate.*"))
    if not candidates:
        raise WatchError(
            "there is nothing to accept: no newer specification has been seen",
            watch=watch.name,
        )
    candidate_path = candidates[0]
    text = candidate_path.read_text(encoding="utf-8")
    spec = load_spec(candidate_path)
    version = spec.metadata.version or ""

    store.write_baseline(watch.name, text, suffix=candidate_path.suffix)
    store.write_state(
        watch.name,
        replace(
            store.read_state(watch.name),
            digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            semantic_digest=semantic_digest(spec),
            version=version,
            last_checked=clock(),
            last_status="accepted",
        ),
    )
    logger.info("watch_accepted", watch=watch.name, version=version)
    return version


def exit_code_for(outcomes: Iterable[CheckOutcome]) -> int:
    """Translate a pass over the watches into a process exit code.

    Three answers rather than two, because a cron job wants to distinguish "I
    could not check" from "I checked, and something is waiting for you".

    Returns:
        ``0`` nothing needs anyone, ``1`` at least one check failed, ``2`` at
        least one watch is waiting on a person.
    """
    collected = list(outcomes)
    if any(outcome.status.is_error for outcome in collected):
        return EXIT_CHECK_FAILED
    if any(outcome.status.needs_a_person for outcome in collected):
        return EXIT_ACTION_REQUIRED
    return 0


__all__ = [
    "EXIT_ACTION_REQUIRED",
    "EXIT_CHECK_FAILED",
    "MAX_SPEC_BYTES",
    "CheckPolicy",
    "accept",
    "check_all",
    "check_watch",
    "exit_code_for",
    "semantic_digest",
    "utc_now",
]
