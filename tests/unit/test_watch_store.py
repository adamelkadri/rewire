"""Tests for what a watch keeps on disk, and for the lock that protects it.

Real files and a real lock rather than fakes: the failure this module exists to
prevent — two overlapping cron runs racing on one baseline — is a property of
the filesystem, and a mock of the filesystem would assert only that the code
calls the functions it calls.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from rewire.core.errors import WatchError
from rewire.watch.models import ActedRecord, Watch, WatchAction, WatchState
from rewire.watch.store import MAX_ACTED_RECORDS, WatchStore, validate_name


@pytest.fixture
def store(tmp_path: Path) -> WatchStore:
    return WatchStore(tmp_path / "watch")


def watch(name: str = "orders", **kwargs: object) -> Watch:
    defaults: dict[str, object] = {"source": "https://e.test/o.yaml", "repository": Path("repo")}
    return Watch(name=name, **{**defaults, **kwargs})  # type: ignore[arg-type]


# -------------------------------------------------------------------- names ---


@pytest.mark.parametrize("name", ["orders", "orders-v2", "a.b_c", "x", "9lives"])
def test_a_usable_name_is_returned_unchanged(name: str) -> None:
    assert validate_name(name) == name


@pytest.mark.parametrize("name", ["", "../escape", "a/b", ".hidden", "-lead", "x" * 65, "a b"])
def test_a_name_that_would_have_to_be_escaped_is_refused(name: str) -> None:
    """The name becomes a directory. Validating beats quoting."""
    with pytest.raises(WatchError, match="watch name"):
        validate_name(name)


def test_a_traversing_name_cannot_reach_outside_the_store(store: WatchStore) -> None:
    with pytest.raises(WatchError):
        store.directory("../../etc")


# ----------------------------------------------------------------- watchlist ---


def test_an_empty_store_has_no_watches(store: WatchStore) -> None:
    assert store.load_all() == ()


def test_a_saved_watch_survives_a_round_trip(store: WatchStore) -> None:
    original = watch(action=WatchAction.PULL_REQUEST, packages=("acme",), draft=True, base="trunk")
    store.save(original)
    assert store.load_all() == (original,)
    assert store.get("orders") == original


def test_saving_the_same_name_replaces_rather_than_duplicates(store: WatchStore) -> None:
    store.save(watch())
    store.save(watch(action=WatchAction.MIGRATE))
    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0].action is WatchAction.MIGRATE


def test_watches_keep_the_order_they_were_added(store: WatchStore) -> None:
    for name in ("a", "b", "c"):
        store.save(watch(name))
    assert [item.name for item in store.load_all()] == ["a", "b", "c"]


def test_an_unknown_name_is_a_readable_error(store: WatchStore) -> None:
    with pytest.raises(WatchError, match="no watch by that name"):
        store.get("absent")
    with pytest.raises(WatchError, match="no watch by that name"):
        store.remove("absent")


def test_a_corrupt_watchlist_is_raised_rather_than_treated_as_empty(store: WatchStore) -> None:
    """Reading it as "no watches" would silently stop every monitor."""
    store.save(watch())
    store.watchlist_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(WatchError, match="could not be read"):
        store.load_all()


def test_a_watchlist_that_is_not_a_list_is_refused(store: WatchStore) -> None:
    store.save(watch())
    store.watchlist_path.write_text('{"orders": {}}', encoding="utf-8")
    with pytest.raises(WatchError, match="must be a list"):
        store.load_all()


def test_a_watch_with_an_unknown_action_is_refused(store: WatchStore) -> None:
    store.save(watch())
    payload = json.loads(store.watchlist_path.read_text(encoding="utf-8"))
    payload[0]["action"] = "merge_it_for_me"
    store.watchlist_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(WatchError, match="unknown watch action"):
        store.load_all()


def test_a_watch_missing_its_source_is_refused(store: WatchStore) -> None:
    store.watchlist_path.parent.mkdir(parents=True, exist_ok=True)
    store.watchlist_path.write_text('[{"name": "orders"}]', encoding="utf-8")
    with pytest.raises(WatchError, match="needs both a name and a source"):
        store.load_all()


def test_removing_a_watch_keeps_its_baseline_by_default(store: WatchStore) -> None:
    """Re-adding it then picks up where it left off rather than adopting today."""
    store.save(watch())
    store.write_baseline("orders", "openapi: 3.0.3\n")
    store.remove("orders")
    assert store.load_all() == ()
    assert store.baseline_path("orders").exists()


def test_removing_with_forget_deletes_the_baseline(store: WatchStore) -> None:
    store.save(watch())
    store.write_baseline("orders", "openapi: 3.0.3\n")
    store.write_state("orders", WatchState(digest="abc"))
    store.remove("orders", forget_state=True)
    assert not store.directory("orders").exists()


# --------------------------------------------------------------------- state ---


def test_state_is_empty_before_the_first_check(store: WatchStore) -> None:
    state = store.read_state("orders")
    assert state == WatchState()
    assert state.has_baseline is False


def test_state_survives_a_round_trip(store: WatchStore) -> None:
    state = WatchState(
        digest="d",
        semantic_digest="s",
        version="2.0.0",
        etag='"e"',
        last_modified="Mon",
        last_checked="2026-01-01T00:00:00+00:00",
        last_status="changes_found",
        acted={"d": ActedRecord(digest="d", at="2026-01-01", status="verified", run_id="r1")},
    )
    store.write_state("orders", state)
    assert store.read_state("orders") == state


def test_a_corrupt_state_file_is_raised_rather_than_read_as_no_baseline(store: WatchStore) -> None:
    """Reading it as having no baseline would adopt whatever is upstream now."""
    store.write_state("orders", WatchState(digest="d"))
    (store.directory("orders") / "state.json").write_text("{truncated", encoding="utf-8")
    with pytest.raises(WatchError, match="could not be read"):
        store.read_state("orders")


def test_a_state_file_that_is_not_an_object_is_refused(store: WatchStore) -> None:
    store.write_state("orders", WatchState(digest="d"))
    (store.directory("orders") / "state.json").write_text("[]", encoding="utf-8")
    with pytest.raises(WatchError, match="must be an object"):
        store.read_state("orders")


def test_the_acted_history_is_trimmed_oldest_first(store: WatchStore) -> None:
    acted = {
        f"digest{index:03d}": ActedRecord(
            digest=f"digest{index:03d}", at=f"2026-01-{index % 28 + 1:02d}T{index:02d}", status="x"
        )
        for index in range(MAX_ACTED_RECORDS + 10)
    }
    store.write_state("orders", WatchState(digest="d", acted=acted))
    kept = store.read_state("orders").acted
    assert len(kept) == MAX_ACTED_RECORDS
    newest = sorted(acted.values(), key=lambda record: record.at)[-1]
    assert newest.digest in kept


def test_nothing_temporary_is_left_behind(store: WatchStore) -> None:
    """The writes are atomic, so a killed check cannot leave a truncated file."""
    store.save(watch())
    store.write_state("orders", WatchState(digest="d"))
    store.write_baseline("orders", "openapi: 3.0.3\n")
    assert not list(store.root.rglob("*.tmp*"))


# ------------------------------------------------------------------ baseline ---


def test_a_baseline_replaces_any_earlier_one_whatever_its_extension(store: WatchStore) -> None:
    """Two baselines would make "the baseline" a question rather than a fact."""
    store.write_baseline("orders", "a", suffix=".yaml")
    store.write_baseline("orders", "b", suffix=".json")
    stored = sorted(store.directory("orders").glob("baseline.*"))
    assert [path.name for path in stored] == ["baseline.json"]
    assert store.baseline_path("orders").read_text(encoding="utf-8") == "b"


def test_the_baseline_path_of_an_unseen_watch_is_a_default_name(store: WatchStore) -> None:
    assert store.baseline_path("orders").name == "baseline.yaml"


def test_a_candidate_is_kept_so_the_two_documents_can_be_compared(store: WatchStore) -> None:
    store.write_baseline("orders", "old")
    path = store.write_candidate("orders", "new", suffix=".json")
    assert path.read_text(encoding="utf-8") == "new"
    assert store.baseline_path("orders").read_text(encoding="utf-8") == "old"


# ---------------------------------------------------------------------- lock ---


def test_the_lock_is_held_for_the_body_and_released_after(store: WatchStore) -> None:
    with store.lock("orders") as held:
        assert held is True
        assert (store.directory("orders") / "check.lock").exists()
    assert not (store.directory("orders") / "check.lock").exists()


def test_the_lock_is_released_even_when_the_body_fails(store: WatchStore) -> None:
    with pytest.raises(RuntimeError), store.lock("orders") as held:
        assert held
        raise RuntimeError("the check exploded")
    assert not (store.directory("orders") / "check.lock").exists()


def test_a_second_check_of_the_same_watch_is_refused_the_lock(store: WatchStore) -> None:
    """The overlapping-cron case: skipped, not raised, and not run twice."""
    with store.lock("orders") as first, store.lock("orders") as second:
        assert first is True
        assert second is False


def test_a_lock_left_by_a_dead_process_is_taken_over(store: WatchStore) -> None:
    """Otherwise a killed run stops the watch forever and gives no reason."""
    path = store.directory("orders") / "check.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{_unused_pid()}\n", encoding="utf-8")
    with store.lock("orders") as held:
        assert held is True


def test_a_lock_with_no_readable_owner_is_taken_over(store: WatchStore) -> None:
    path = store.directory("orders") / "check.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    with store.lock("orders") as held:
        assert held is True


def test_a_lock_held_by_this_process_is_not_taken_over(store: WatchStore) -> None:
    path = store.directory("orders") / "check.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    with store.lock("orders") as held:
        assert held is False


def _unused_pid() -> int:
    """Find a pid that is not running, so the lock under it is genuinely stale."""
    for candidate in range(4_000_000, 4_000_100):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:  # pragma: no cover - alive, owned by someone else
            continue
    raise AssertionError("no free pid found")  # pragma: no cover - 100 candidates


def test_a_state_replacement_does_not_mutate_the_original(store: WatchStore) -> None:
    """The models are frozen; this is the property the check logic relies on."""
    state = WatchState(digest="a")
    updated = replace(state, digest="b")
    assert state.digest == "a"
    assert updated.digest == "b"


def test_a_lock_owned_by_another_user_is_left_alone(
    store: WatchStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live process this one cannot signal is still a live process."""

    def denied(_pid: int, _signal: int) -> None:
        raise PermissionError("not yours")

    path = store.directory("orders") / "check.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("12345\n", encoding="utf-8")
    monkeypatch.setattr("rewire.watch.store.os.kill", denied)
    with store.lock("orders") as held:
        assert held is False


def test_a_write_that_cannot_complete_is_a_readable_error(store: WatchStore) -> None:
    """And leaves no temporary file behind for the next check to trip over."""
    directory = store.directory("orders")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "state.json").mkdir()  # a directory where the file must go

    with pytest.raises(WatchError, match=r"could not write state\.json"):
        store.write_state("orders", WatchState(digest="d"))
    assert not list(directory.glob("*.tmp*"))


def test_getting_a_watch_looks_past_the_ones_that_do_not_match(store: WatchStore) -> None:
    store.save(watch("first"))
    store.save(watch("second"))
    assert store.get("second").name == "second"
