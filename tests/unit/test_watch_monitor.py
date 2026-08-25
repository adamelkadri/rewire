"""Tests for the decision a check makes, with the network and the model removed.

Everything expensive is injected — the fetch, the migration, the publish, the
clock — so every branch of the state machine can be driven exactly, including
the ones that must *not* happen. Several tests here assert an absence: that a
model was not called, that a pull request was not opened twice, that the
baseline did not move. Those are the properties that make a monitor safe to
leave running, and none of them can be observed by looking at what a check
returns.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from rewire.changes.spec import parse_spec_text
from rewire.core.errors import GitError, WatchError
from rewire.gitio.github import PullRequest
from rewire.services.migrate import MigrationOutcome, MigrationRequest, MigrationStatus
from rewire.services.publish import PublishOutcome, PublishRequest, PublishStatus
from rewire.watch import monitor
from rewire.watch.models import ActedRecord, CheckOutcome, CheckStatus, Watch, WatchAction
from rewire.watch.monitor import (
    EXIT_ACTION_REQUIRED,
    EXIT_CHECK_FAILED,
    CheckPolicy,
    accept,
    check_all,
    check_watch,
    exit_code_for,
    semantic_digest,
)
from rewire.watch.source import Fetched
from rewire.watch.store import WatchStore


def spec(version: str = "1.0.0", *, field: str = "customer_name", extra: str = "") -> str:
    """A one-operation specification, small enough to reason about."""
    properties = {field: {"type": "string"}}
    if extra:
        properties[extra] = {"type": "string"}
    document = {
        "openapi": "3.0.3",
        "info": {"title": "Orders API", "version": version},
        "paths": {
            "/orders": {
                "get": {
                    "operationId": "listOrders",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object", "properties": properties}
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    return yaml.safe_dump(document, sort_keys=True)


V1 = spec()
V1_ADDITIVE = spec("1.1.0", extra="currency")
V2_BREAKING = spec("2.0.0", field="customer")


class Fetcher:
    """Hands back scripted documents and records what it was asked for."""

    def __init__(self, *documents: str | Fetched) -> None:
        self.calls: list[dict[str, object]] = []
        self._documents = list(documents)

    def __call__(self, source: str, **kwargs: object) -> Fetched:
        self.calls.append({"source": source, **kwargs})
        item = self._documents.pop(0) if len(self._documents) > 1 else self._documents[0]
        if isinstance(item, Fetched):
            return item
        if isinstance(item, WatchError):  # pragma: no cover - raised below instead
            raise item
        return Fetched(text=item, source=source, suffix=".yaml")


class Failing:
    """A fetcher that always refuses."""

    def __init__(self, message: str = "connection refused") -> None:
        self.message = message

    def __call__(self, source: str, **_kwargs: object) -> Fetched:
        raise WatchError(self.message, url=source)


class Migrator:
    """Stands in for the pipeline, recording every request it was given."""

    def __init__(self, status: MigrationStatus = MigrationStatus.VERIFIED) -> None:
        self.requests: list[MigrationRequest] = []
        self.status = status
        self.error: Exception | None = None

    def __call__(self, request: MigrationRequest) -> MigrationOutcome:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return MigrationOutcome(run_id="run-1", status=self.status)


class Publisher:
    """Stands in for publishing, recording every request it was given."""

    def __init__(self, status: PublishStatus = PublishStatus.PUBLISHED) -> None:
        self.requests: list[PublishRequest] = []
        self.status = status

    def __call__(self, outcome: MigrationOutcome, request: PublishRequest) -> PublishOutcome:
        self.requests.append(request)
        pull_request = (
            PullRequest(url="https://github.com/acme/widgets/pull/7", number=7, branch="rewire/x")
            if self.status is PublishStatus.PUBLISHED
            else None
        )
        return PublishOutcome(status=self.status, branch="rewire/x", pull_request=pull_request)


def clock(stamp: str = "2026-01-01T00:00:00+00:00") -> object:
    return lambda: stamp


@pytest.fixture
def store(tmp_path: Path) -> WatchStore:
    return WatchStore(tmp_path / "watch")


def watch(**kwargs: object) -> Watch:
    defaults: dict[str, object] = {
        "name": "orders",
        "source": "https://e.test/o.yaml",
        "repository": Path("repo"),
    }
    return Watch(**{**defaults, **kwargs})  # type: ignore[arg-type]


def check(
    store: WatchStore,
    fetcher: object,
    *,
    subject: Watch | None = None,
    migrate: object = None,
    publish: object = None,
    policy: CheckPolicy | None = None,
) -> CheckOutcome:
    return check_watch(
        subject or watch(),
        store=store,
        policy=policy,
        migrate=migrate,  # type: ignore[arg-type]
        publish=publish,  # type: ignore[arg-type]
        fetcher=fetcher,  # type: ignore[arg-type]
        clock=clock(),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------- cheap questions ---


def test_the_first_check_adopts_a_baseline_and_does_nothing_else(store: WatchStore) -> None:
    outcome = check(store, Fetcher(V1))
    assert outcome.status is CheckStatus.ADOPTED
    assert outcome.version == "1.0.0"
    assert outcome.baseline_advanced is True
    assert store.baseline_path("orders").read_text(encoding="utf-8") == V1
    assert store.read_state("orders").version == "1.0.0"


def test_an_unchanged_document_is_recognised_by_its_digest(store: WatchStore) -> None:
    check(store, Fetcher(V1))
    outcome = check(store, Fetcher(V1))
    assert outcome.status is CheckStatus.UNCHANGED
    assert outcome.version == "1.0.0"


def test_a_304_settles_it_without_a_body_at_all(store: WatchStore) -> None:
    check(store, Fetcher(V1))
    outcome = check(store, Fetcher(Fetched(not_modified=True, etag='"e"')))
    assert outcome.status is CheckStatus.UNCHANGED
    assert store.read_state("orders").etag == '"e"'


def test_the_stored_validators_are_offered_on_the_next_check(store: WatchStore) -> None:
    check(store, Fetcher(Fetched(text=V1, etag='"abc"', last_modified="Mon")))
    fetcher = Fetcher(V1)
    check(store, fetcher)
    assert fetcher.calls[0]["etag"] == '"abc"'
    assert fetcher.calls[0]["last_modified"] == "Mon"


def test_a_regenerated_document_with_the_same_meaning_is_not_a_change(store: WatchStore) -> None:
    """A vendor re-exporting YAML as JSON must not wake anybody up."""
    check(store, Fetcher(V1))
    as_json = json.dumps(yaml.safe_load(V1), indent=2, sort_keys=False)
    outcome = check(store, Fetcher(Fetched(text=as_json, suffix=".json")))
    assert outcome.status is CheckStatus.REFORMATTED
    assert outcome.changes is None
    assert store.baseline_path("orders").name == "baseline.json"


def test_a_change_that_cannot_break_a_caller_advances_the_baseline(store: WatchStore) -> None:
    check(store, Fetcher(V1))
    outcome = check(store, Fetcher(V1_ADDITIVE))
    assert outcome.status is CheckStatus.NO_BREAKING_CHANGES
    assert outcome.baseline_advanced is True
    assert store.baseline_path("orders").read_text(encoding="utf-8") == V1_ADDITIVE
    assert store.read_state("orders").version == "1.1.0"


def test_semantic_digest_ignores_how_a_document_is_written() -> None:
    as_json = json.dumps(yaml.safe_load(V1))
    assert semantic_digest(parse_spec_text(V1)) == semantic_digest(parse_spec_text(as_json))
    assert semantic_digest(parse_spec_text(V1)) != semantic_digest(parse_spec_text(V2_BREAKING))


# --------------------------------------------------------------- reporting ---


def test_a_breaking_change_is_reported_and_the_baseline_stays_put(store: WatchStore) -> None:
    check(store, Fetcher(V1))
    outcome = check(store, Fetcher(V2_BREAKING))
    assert outcome.status is CheckStatus.CHANGES_FOUND
    assert outcome.breaking > 0
    assert outcome.baseline_advanced is False
    assert store.baseline_path("orders").read_text(encoding="utf-8") == V1


def test_the_two_documents_behind_the_finding_are_both_kept(store: WatchStore) -> None:
    check(store, Fetcher(V1))
    check(store, Fetcher(V2_BREAKING))
    directory = store.directory("orders")
    assert (directory / "baseline.yaml").read_text(encoding="utf-8") == V1
    assert (directory / "candidate.yaml").read_text(encoding="utf-8") == V2_BREAKING


def test_a_reporting_watch_says_the_same_thing_until_somebody_acts(store: WatchStore) -> None:
    """Reporting is not acting. The finding is still true on the next check."""
    check(store, Fetcher(V1))
    first = check(store, Fetcher(V2_BREAKING))
    second = check(store, Fetcher(V2_BREAKING))
    assert first.status is second.status is CheckStatus.CHANGES_FOUND
    assert store.read_state("orders").acted == {}


def test_accepting_makes_it_stop(store: WatchStore) -> None:
    check(store, Fetcher(V1))
    check(store, Fetcher(V2_BREAKING))
    assert accept(watch(), store=store, clock=clock()) == "2.0.0"  # type: ignore[arg-type]
    assert check(store, Fetcher(V2_BREAKING)).status is CheckStatus.UNCHANGED
    assert store.read_state("orders").version == "2.0.0"


def test_accepting_with_nothing_newer_seen_is_refused(store: WatchStore) -> None:
    check(store, Fetcher(V1))
    with pytest.raises(WatchError, match="nothing to accept"):
        accept(watch(), store=store, clock=clock())  # type: ignore[arg-type]


# ------------------------------------------------------------------ acting ---


def test_a_migrating_watch_runs_the_pipeline_against_the_two_stored_documents(
    store: WatchStore,
) -> None:
    check(store, Fetcher(V1))
    migrator = Migrator()
    outcome = check(
        store, Fetcher(V2_BREAKING), subject=watch(action=WatchAction.MIGRATE), migrate=migrator
    )
    assert outcome.status is CheckStatus.MIGRATED
    request = migrator.requests[0]
    assert request.old_spec.read_text(encoding="utf-8") == V1
    assert request.new_spec.read_text(encoding="utf-8") == V2_BREAKING
    assert request.apply is False


def test_a_watch_never_writes_into_the_working_tree(store: WatchStore) -> None:
    """An unattended monitor leaving uncommitted edits behind is the surprise."""
    check(store, Fetcher(V1))
    migrator = Migrator()
    check(store, Fetcher(V2_BREAKING), subject=watch(action=WatchAction.MIGRATE), migrate=migrator)
    assert all(request.apply is False for request in migrator.requests)


def test_a_verified_patch_does_not_advance_the_baseline(store: WatchStore) -> None:
    """The code has not changed. Recording otherwise would be a lie about it."""
    check(store, Fetcher(V1))
    check(
        store,
        Fetcher(V2_BREAKING),
        subject=watch(action=WatchAction.MIGRATE),
        migrate=Migrator(),
    )
    assert store.baseline_path("orders").read_text(encoding="utf-8") == V1
    assert store.read_state("orders").version == "1.0.0"


def test_a_version_that_was_acted_on_is_not_acted_on_again(store: WatchStore) -> None:
    """The whole reason a monitor on an hourly cron is affordable."""
    check(store, Fetcher(V1))
    migrator = Migrator()
    subject = watch(action=WatchAction.MIGRATE)
    check(store, Fetcher(V2_BREAKING), subject=subject, migrate=migrator)
    outcome = check(store, Fetcher(V2_BREAKING), subject=subject, migrate=migrator)

    assert outcome.status is CheckStatus.ALREADY_ACTED
    assert outcome.previous is not None
    assert outcome.previous.run_id == "run-1"
    assert len(migrator.requests) == 1


def test_retry_asks_again(store: WatchStore) -> None:
    check(store, Fetcher(V1))
    migrator = Migrator()
    subject = watch(action=WatchAction.MIGRATE)
    check(store, Fetcher(V2_BREAKING), subject=subject, migrate=migrator)
    check(
        store,
        Fetcher(V2_BREAKING),
        subject=subject,
        migrate=migrator,
        policy=CheckPolicy(retry=True),
    )
    assert len(migrator.requests) == 2


def test_a_failed_migration_is_recorded_so_it_is_not_repeated_hourly(store: WatchStore) -> None:
    check(store, Fetcher(V1))
    migrator = Migrator()
    migrator.error = GitError("docker is not running")
    subject = watch(action=WatchAction.MIGRATE)

    outcome = check(store, Fetcher(V2_BREAKING), subject=subject, migrate=migrator)
    assert outcome.status is CheckStatus.FAILED
    assert "docker" in outcome.reason

    repeat = check(store, Fetcher(V2_BREAKING), subject=subject, migrate=migrator)
    assert repeat.status is CheckStatus.ALREADY_ACTED
    assert len(migrator.requests) == 1


def test_a_watch_that_cannot_act_says_so_rather_than_pretending(store: WatchStore) -> None:
    check(store, Fetcher(V1))
    outcome = check(store, Fetcher(V2_BREAKING), subject=watch(action=WatchAction.MIGRATE))
    assert outcome.status is CheckStatus.FAILED
    assert "no way to do it" in outcome.reason


# ------------------------------------------------------------- pull request ---


def test_a_pull_request_watch_publishes_and_records_where(
    store: WatchStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitor, "check_publishable", lambda _root: "")
    check(store, Fetcher(V1))
    publisher = Publisher()
    outcome = check(
        store,
        Fetcher(V2_BREAKING),
        subject=watch(action=WatchAction.PULL_REQUEST, draft=True, base="trunk"),
        migrate=Migrator(),
        publish=publisher,
    )
    assert outcome.status is CheckStatus.MIGRATED
    assert publisher.requests[0].draft is True
    assert publisher.requests[0].base == "trunk"
    record = store.read_state("orders").acted
    assert next(iter(record.values())).pull_request.endswith("/pull/7")


def test_an_open_pull_request_does_not_advance_the_baseline(
    store: WatchStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is a proposal about someone else's repository, not a fact about it."""
    monkeypatch.setattr(monitor, "check_publishable", lambda _root: "")
    check(store, Fetcher(V1))
    check(
        store,
        Fetcher(V2_BREAKING),
        subject=watch(action=WatchAction.PULL_REQUEST),
        migrate=Migrator(),
        publish=Publisher(),
    )
    assert store.baseline_path("orders").read_text(encoding="utf-8") == V1


def test_publishing_preconditions_are_checked_before_the_model_is_called(
    store: WatchStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every one is free to check. Learning them after an agent run is not."""
    monkeypatch.setattr(monitor, "check_publishable", lambda _root: "the working tree is dirty")
    check(store, Fetcher(V1))
    migrator = Migrator()
    outcome = check(
        store,
        Fetcher(V2_BREAKING),
        subject=watch(action=WatchAction.PULL_REQUEST),
        migrate=migrator,
        publish=Publisher(),
    )
    assert outcome.status is CheckStatus.FAILED
    assert "dirty" in outcome.reason
    assert migrator.requests == []


def test_a_refused_publication_is_recorded_with_its_reason(
    store: WatchStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitor, "check_publishable", lambda _root: "")
    check(store, Fetcher(V1))
    outcome = check(
        store,
        Fetcher(V2_BREAKING),
        subject=watch(action=WatchAction.PULL_REQUEST),
        migrate=Migrator(MigrationStatus.UNVERIFIED),
        publish=Publisher(PublishStatus.REFUSED),
    )
    assert outcome.publication is not None
    assert outcome.publication.status is PublishStatus.REFUSED
    assert next(iter(store.read_state("orders").acted.values())).status == "refused"


def test_a_pull_request_watch_with_no_publisher_says_so(store: WatchStore) -> None:
    check(store, Fetcher(V1))
    outcome = check(
        store,
        Fetcher(V2_BREAKING),
        subject=watch(action=WatchAction.PULL_REQUEST),
        migrate=Migrator(),
    )
    assert outcome.status is CheckStatus.FAILED


# ----------------------------------------------------------------- failures ---


def test_an_unreachable_source_is_reported_rather_than_raised(store: WatchStore) -> None:
    """One unreachable vendor must not stop the other watches from running."""
    outcome = check(store, Failing("connection refused"))
    assert outcome.status is CheckStatus.FAILED
    assert "connection refused" in outcome.reason
    assert store.read_state("orders").has_baseline is False


def test_a_failed_fetch_still_records_that_a_check_happened(store: WatchStore) -> None:
    check(store, Failing())
    state = store.read_state("orders")
    assert state.last_status == CheckStatus.FAILED.value
    assert state.last_checked == "2026-01-01T00:00:00+00:00"


def test_an_unparseable_document_does_not_teach_the_next_check_to_skip_it(
    store: WatchStore,
) -> None:
    """Storing its ETag would make the next 304 mean "the good copy is current"."""
    check(store, Fetcher(V1))
    outcome = check(store, Fetcher(Fetched(text="not a specification", etag='"bad"')))
    assert outcome.status is CheckStatus.FAILED
    assert store.read_state("orders").etag == ""


def test_an_edited_baseline_is_reported_rather_than_raised(store: WatchStore) -> None:
    check(store, Fetcher(V1))
    store.baseline_path("orders").write_text("not a specification", encoding="utf-8")
    outcome = check(store, Fetcher(V2_BREAKING))
    assert outcome.status is CheckStatus.FAILED


def test_a_disabled_watch_is_skipped_without_touching_anything(store: WatchStore) -> None:
    fetcher = Fetcher(V1)
    outcome = check(store, fetcher, subject=watch(enabled=False))
    assert outcome.status is CheckStatus.SKIPPED
    assert fetcher.calls == []


def test_an_overlapping_check_is_skipped_rather_than_run_twice(store: WatchStore) -> None:
    with store.lock("orders"):
        outcome = check(store, Fetcher(V1))
    assert outcome.status is CheckStatus.SKIPPED
    assert "already running" in outcome.reason


# -------------------------------------------------------------------- pass ---


def test_every_watch_in_a_pass_is_checked_even_after_one_fails(store: WatchStore) -> None:
    good = watch(name="good", source="a")
    bad = watch(name="bad", source="b")

    def fetcher(source: str, **_kwargs: object) -> Fetched:
        if source == "b":
            raise WatchError("gone", url=source)
        return Fetched(text=V1, suffix=".yaml")

    outcomes = check_all([good, bad], store=store, fetcher=fetcher, clock=clock())  # type: ignore[arg-type]
    assert [outcome.status for outcome in outcomes] == [CheckStatus.ADOPTED, CheckStatus.FAILED]


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([CheckStatus.UNCHANGED, CheckStatus.NO_BREAKING_CHANGES], 0),
        ([CheckStatus.UNCHANGED, CheckStatus.CHANGES_FOUND], EXIT_ACTION_REQUIRED),
        ([CheckStatus.MIGRATED], EXIT_ACTION_REQUIRED),
        ([CheckStatus.ALREADY_ACTED], EXIT_ACTION_REQUIRED),
        ([CheckStatus.CHANGES_FOUND, CheckStatus.FAILED], EXIT_CHECK_FAILED),
        ([], 0),
    ],
)
def test_the_exit_code_says_which_of_the_three_answers_it_is(
    statuses: list[CheckStatus], expected: int
) -> None:
    """A failure outranks a finding: "I could not look" is not "nothing found"."""
    outcomes = [CheckOutcome(watch=watch(), status=status) for status in statuses]
    assert exit_code_for(outcomes) == expected


def test_a_skipped_watch_needs_nobody(store: WatchStore) -> None:
    outcome = CheckOutcome(watch=watch(), status=CheckStatus.SKIPPED)
    assert exit_code_for([outcome]) == 0


# ------------------------------------------------------------------ summary ---


@pytest.mark.parametrize("status", list(CheckStatus))
def test_every_status_has_a_sentence(status: CheckStatus) -> None:
    outcome = CheckOutcome(
        watch=watch(),
        status=status,
        migration=MigrationOutcome(run_id="r", status=MigrationStatus.VERIFIED),
        previous=ActedRecord(digest="d", at="now", status="verified", run_id="r"),
    )
    assert outcome.summary_line()


def test_an_acted_record_names_where_to_look() -> None:
    with_pull_request = ActedRecord(
        digest="d", at="now", status="published", pull_request="https://e.test/pull/1"
    )
    assert "https://e.test/pull/1" in with_pull_request.describe()
    assert "run r" in ActedRecord(digest="d", at="now", status="verified", run_id="r").describe()
    assert ActedRecord(digest="d", at="now", status="failed").describe() == "failed at now"
