"""Tests for the run record, which Phase 13 turns into an interface.

The file has been written since Phase 7 and read by nothing, which is exactly the
window in which a format can be changed freely and exactly the window that is
closing. What is covered here is the behaviour that matters once something does
read it: that a record survives a round trip, that a version it does not
understand is refused rather than guessed at, and that failing to write one never
costs the run its answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rewire.services.migrate import MigrationOutcome
from rewire.services.record import (
    RECORD_FILENAME,
    RECORD_VERSION,
    AttemptRecord,
    MigrationRecord,
    MigrationStatus,
    RecordError,
    read_all,
    read_record,
    read_run,
    record_path,
    write_record,
)


def record(**kwargs: object) -> MigrationRecord:
    base: dict[str, object] = {"run_id": "abc123", "status": MigrationStatus.VERIFIED}
    return MigrationRecord(**{**base, **kwargs})  # type: ignore[arg-type]


# ------------------------------------------------------------------- shape ---


def test_a_record_carries_the_version_that_wrote_it() -> None:
    """Without it, the first format change makes every old file indistinguishable."""
    assert record().version == RECORD_VERSION
    assert json.loads(record().to_json())["version"] == RECORD_VERSION


def test_a_record_survives_a_round_trip_through_disk(tmp_path: Path) -> None:
    original = record(
        summary="patch verified across 2 file(s)",
        duration_seconds=12.5,
        files_written=("app.py", "client.py"),
        affected_locations=3,
        attempts=(
            AttemptRecord(number=1, verdict="regressed", files=2, tokens=900),
            AttemptRecord(number=2, verdict="verified", files=2, tokens=1200),
        ),
        repaired=True,
        total_tokens=2100,
        total_cost_usd=0.0421,
    )
    path = tmp_path / RECORD_FILENAME
    path.write_text(original.to_json(), encoding="utf-8")
    assert read_record(path) == original


def test_a_record_agrees_with_the_status_about_success() -> None:
    assert record(status=MigrationStatus.NO_AFFECTED_CODE).succeeded is True
    assert record(status=MigrationStatus.UNVERIFIED).succeeded is False


def test_a_record_is_built_from_a_finished_run() -> None:
    outcome = MigrationOutcome(run_id="run-1", status=MigrationStatus.NO_BREAKING_CHANGES)
    built = MigrationRecord.of(outcome)
    assert built.run_id == "run-1"
    assert built.status is MigrationStatus.NO_BREAKING_CHANGES
    assert built.summary == outcome.summary_line()
    assert built.attempts == ()
    assert built.total_cost_usd is None


# ----------------------------------------------------------------- reading ---


def test_reading_a_newer_version_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    """A reader that shrugs is how a benchmark computes a number from fields
    that no longer mean what it thinks they mean.
    """
    payload = json.loads(record().to_json())
    payload["version"] = RECORD_VERSION + 1
    path = tmp_path / RECORD_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RecordError, match="newer version"):
        read_record(path)


def test_a_record_with_no_version_is_refused(tmp_path: Path) -> None:
    """Every record this build wrote has one, so a missing one is not ours."""
    payload = json.loads(record().to_json())
    del payload["version"]
    path = tmp_path / RECORD_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RecordError, match="newer version"):
        read_record(path)


def test_a_missing_record_is_a_readable_error(tmp_path: Path) -> None:
    with pytest.raises(RecordError, match="could not be read"):
        read_record(tmp_path / "absent.json")


def test_an_unparseable_record_is_a_readable_error(tmp_path: Path) -> None:
    path = tmp_path / RECORD_FILENAME
    path.write_text("{truncated", encoding="utf-8")
    with pytest.raises(RecordError, match="could not be read"):
        read_record(path)


def test_a_record_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    path = tmp_path / RECORD_FILENAME
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(RecordError, match="must be an object"):
        read_record(path)


def test_a_record_missing_a_required_field_is_refused(tmp_path: Path) -> None:
    path = tmp_path / RECORD_FILENAME
    path.write_text(json.dumps({"version": RECORD_VERSION}), encoding="utf-8")
    with pytest.raises(RecordError, match="not valid"):
        read_record(path)


def test_an_unknown_status_is_refused_rather_than_kept_as_text(tmp_path: Path) -> None:
    """The status drives every decision downstream. A typo must not survive."""
    payload = json.loads(record().to_json())
    payload["status"] = "probably_fine"
    path = tmp_path / RECORD_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RecordError, match="not valid"):
        read_record(path)


# ------------------------------------------------------------------ writing ---


def test_a_run_is_written_and_read_back_by_identifier(tmp_path: Path) -> None:
    outcome = MigrationOutcome(run_id="run-7", status=MigrationStatus.VERIFIED)
    written = write_record(tmp_path, outcome)
    assert written == record_path(tmp_path, "run-7")
    assert read_run(tmp_path, "run-7").run_id == "run-7"


def test_a_record_that_cannot_be_written_does_not_cost_the_run_its_answer(
    tmp_path: Path,
) -> None:
    """The artefact is evidence about the work, not the work."""
    (tmp_path / "run-7").write_text("a file where the directory must go", encoding="utf-8")
    outcome = MigrationOutcome(run_id="run-7", status=MigrationStatus.VERIFIED)
    assert write_record(tmp_path, outcome) is None


def test_reading_a_run_that_never_happened_is_a_readable_error(tmp_path: Path) -> None:
    with pytest.raises(RecordError, match="could not be read"):
        read_run(tmp_path, "never-ran")


def test_every_run_is_readable_together(tmp_path: Path) -> None:
    for index in range(3):
        write_record(
            tmp_path, MigrationOutcome(run_id=f"run-{index}", status=MigrationStatus.VERIFIED)
        )
    assert [item.run_id for item in read_all(tmp_path)] == ["run-0", "run-1", "run-2"]


def test_one_corrupt_record_does_not_make_the_history_unreadable(tmp_path: Path) -> None:
    """Asking for one record by name still raises; asking for all of them does not."""
    write_record(tmp_path, MigrationOutcome(run_id="good", status=MigrationStatus.VERIFIED))
    broken = tmp_path / "bad"
    broken.mkdir()
    (broken / RECORD_FILENAME).write_text("{truncated", encoding="utf-8")

    assert [item.run_id for item in read_all(tmp_path)] == ["good"]
    with pytest.raises(RecordError):
        read_run(tmp_path, "bad")


def test_reading_an_empty_directory_finds_nothing(tmp_path: Path) -> None:
    assert read_all(tmp_path) == ()
