"""Tests for the watch models, and for what they do with a file someone edited.

The state file is JSON on disk, which makes it something a person can open and
change. These cover the shapes that arrive when they do: a field of the wrong
type, a record that is not a record, a payload from an older version. The rule
throughout is that a malformed *entry* is dropped and a malformed *file* is
raised, because dropping the file would silently reset the baseline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rewire.core.errors import WatchError
from rewire.watch.models import ActedRecord, Watch, WatchAction, WatchState


def test_an_action_knows_whether_it_costs_anything() -> None:
    assert WatchAction.REPORT.calls_a_model is False
    assert WatchAction.MIGRATE.calls_a_model is True
    assert WatchAction.PULL_REQUEST.calls_a_model is True


def test_a_watch_survives_a_round_trip_through_json_shapes() -> None:
    original = Watch(
        name="orders",
        source="https://e.test/o.yaml",
        repository=Path("repo"),
        packages=("acme", "acme-client"),
        action=WatchAction.PULL_REQUEST,
        base="trunk",
        draft=True,
        max_attempts=5,
        enabled=False,
    )
    assert Watch.from_dict(original.to_dict()) == original


def test_a_watch_from_an_older_payload_gets_the_current_defaults() -> None:
    """Adding a field must not make every stored watch unreadable."""
    minimal = Watch.from_dict({"name": "orders", "source": "s"})
    assert minimal.action is WatchAction.REPORT
    assert minimal.repository == Path()
    assert minimal.packages == ()
    assert minimal.max_attempts == 3
    assert minimal.enabled is True


@pytest.mark.parametrize("payload", [{"name": "orders"}, {"source": "s"}, {}])
def test_a_watch_without_a_name_and_a_source_is_refused(payload: dict[str, object]) -> None:
    with pytest.raises(WatchError, match="needs both a name and a source"):
        Watch.from_dict(payload)


def test_packages_that_are_not_a_list_are_dropped_rather_than_guessed() -> None:
    assert Watch.from_dict({"name": "o", "source": "s", "packages": "acme"}).packages == ()


def test_max_attempts_written_as_a_string_is_still_a_number() -> None:
    assert Watch.from_dict({"name": "o", "source": "s", "max_attempts": "5"}).max_attempts == 5
    assert Watch.from_dict({"name": "o", "source": "s", "max_attempts": None}).max_attempts == 3


def test_state_survives_a_round_trip() -> None:
    state = WatchState(
        digest="d",
        semantic_digest="s",
        version="1.0.0",
        acted={"d": ActedRecord(digest="d", at="now", status="verified", run_id="r")},
    )
    assert WatchState.from_dict(state.to_dict()) == state


def test_an_acted_history_of_the_wrong_shape_is_dropped_not_raised() -> None:
    """The baseline is what matters. A damaged history costs one repeated run."""
    assert WatchState.from_dict({"digest": "d", "acted": "not a mapping"}).acted == {}
    assert WatchState.from_dict({"digest": "d", "acted": {"x": "not a record"}}).acted == {}


def test_state_with_nothing_in_it_has_no_baseline() -> None:
    assert WatchState().has_baseline is False
    assert WatchState(digest="d").has_baseline is True
