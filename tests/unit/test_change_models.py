"""Tests for the change model contract."""

from __future__ import annotations

import json

import pytest

from rewire.changes.models import (
    MAX_VALUE_CHARS,
    ApiChange,
    ChangeReport,
    ChangeSummary,
    ChangeType,
    Severity,
    SpecRef,
)


def change(
    change_type: ChangeType = ChangeType.PARAMETER_REMOVED,
    severity: Severity = Severity.BREAKING,
    **kwargs: object,
) -> ApiChange:
    return ApiChange(type=change_type, severity=severity, **kwargs)  # type: ignore[arg-type]


def test_severity_ordering() -> None:
    assert Severity.BREAKING.rank < Severity.POTENTIALLY_BREAKING.rank
    assert Severity.POTENTIALLY_BREAKING.rank < Severity.NON_BREAKING.rank


def test_at_least_includes_more_severe_values() -> None:
    assert Severity.BREAKING.at_least(Severity.NON_BREAKING)
    assert Severity.BREAKING.at_least(Severity.BREAKING)
    assert not Severity.NON_BREAKING.at_least(Severity.BREAKING)


def test_field_is_derived_from_field_path() -> None:
    assert change(field_path="usage.completion_tokens").field == "completion_tokens"
    assert change(field_path="choices[]").field == "choices"
    assert change(field_path="max_tokens").field == "max_tokens"


def test_explicit_field_is_not_overwritten() -> None:
    assert change(field="explicit", field_path="a.b").field == "explicit"


def test_no_field_path_leaves_field_unset() -> None:
    assert change().field is None


def test_changes_are_immutable() -> None:
    with pytest.raises(pytest.importorskip("pydantic").ValidationError):
        change().type = ChangeType.ENDPOINT_ADDED  # type: ignore[misc]


def test_is_breaking_matches_severity() -> None:
    assert change(severity=Severity.BREAKING).is_breaking
    assert not change(severity=Severity.POTENTIALLY_BREAKING).is_breaking


def test_serialisation_omits_inapplicable_fields() -> None:
    """The published shape must stay minimal, as documented in the README."""
    payload = change(endpoint="POST /v1/messages", field_path="max_tokens").model_dump(
        mode="json", exclude_none=True, exclude={"detail", "field_path"}
    )
    assert payload == {
        "type": "parameter_removed",
        "severity": "breaking",
        "endpoint": "POST /v1/messages",
        "field": "max_tokens",
    }
    assert json.loads(json.dumps(payload)) == payload


def test_summary_counts_by_severity_and_type() -> None:
    summary = ChangeSummary.from_changes(
        [
            change(endpoint="A"),
            change(ChangeType.PARAMETER_ADDED, Severity.NON_BREAKING, endpoint="A"),
            change(ChangeType.ENDPOINT_REMOVED, Severity.BREAKING, endpoint="B"),
        ]
    )
    assert summary.total == 3
    assert summary.breaking == 2
    assert summary.non_breaking == 1
    assert summary.endpoints_affected == 2
    assert summary.by_type == {"endpoint_removed": 1, "parameter_added": 1, "parameter_removed": 1}


def test_empty_summary() -> None:
    summary = ChangeSummary.from_changes([])
    assert summary.total == 0
    assert summary.by_type == {}


def test_build_sorts_and_summarises() -> None:
    report = ChangeReport.build(
        SpecRef(version="1"),
        SpecRef(version="2"),
        [
            change(ChangeType.PARAMETER_ADDED, Severity.NON_BREAKING, endpoint="B"),
            change(ChangeType.PARAMETER_REMOVED, Severity.BREAKING, endpoint="A"),
        ],
    )
    assert [c.severity for c in report.changes] == [Severity.BREAKING, Severity.NON_BREAKING]
    assert report.summary.total == 2
    assert report.old_spec.version == "1"


def test_sort_is_total_and_stable() -> None:
    """Equal-severity changes must still order deterministically."""
    changes = [
        change(endpoint="B", field_path="z"),
        change(endpoint="A", field_path="b"),
        change(endpoint="A", field_path="a"),
    ]
    report = ChangeReport.build(SpecRef(), SpecRef(), changes)
    assert [(c.endpoint, c.field_path) for c in report.changes] == [
        ("A", "a"),
        ("A", "b"),
        ("B", "z"),
    ]


def test_empty_report_has_no_breaking_changes() -> None:
    report = ChangeReport.build(SpecRef(), SpecRef(), [])
    assert not report.has_breaking_changes
    assert report.filter(Severity.NON_BREAKING) == []


def test_every_change_type_has_a_unique_value() -> None:
    values = [member.value for member in ChangeType]
    assert len(values) == len(set(values))


# ------------------------------------------------------------------ values ---


def test_a_change_renders_the_values_it_carries() -> None:
    """The information was always on the model; it was simply never shown.

    ``detail`` says *that* an enum value was removed. An agent told only that has
    nothing to migrate the value *to*, so the only move left is to invent one --
    which is exactly what sixteen benchmark runs of `05-enum-value-removed`
    observed it doing.
    """
    removed = ApiChange(
        type=ChangeType.REQUEST_SCHEMA_CHANGED,
        severity=Severity.BREAKING,
        field_path="response_format",
        old_value=["json"],
    )
    added = ApiChange(
        type=ChangeType.REQUEST_SCHEMA_CHANGED,
        severity=Severity.NON_BREAKING,
        field_path="response_format",
        new_value=["json_object"],
    )
    assert removed.value_lines() == ["was: 'json'"]
    assert added.value_lines() == ["now: 'json_object'"]


def test_a_change_with_both_values_renders_both() -> None:
    change = ApiChange(
        type=ChangeType.REQUEST_SCHEMA_CHANGED,
        severity=Severity.BREAKING,
        field_path="count",
        old_value="string",
        new_value="integer",
    )
    assert change.value_lines() == ["was: 'string'", "now: 'integer'"]


def test_a_change_with_no_values_renders_nothing() -> None:
    change = ApiChange(type=ChangeType.ENDPOINT_REMOVED, severity=Severity.BREAKING)
    assert change.value_lines() == []


def test_a_false_value_is_still_a_value() -> None:
    """``if not value`` would drop ``false`` and ``0``, which are real values."""
    change = ApiChange(
        type=ChangeType.REQUEST_SCHEMA_CHANGED,
        severity=Severity.BREAKING,
        old_value=False,
        new_value=0,
    )
    assert change.value_lines() == ["was: False", "now: 0"]


def test_a_huge_value_is_truncated_rather_than_flooding_the_prompt() -> None:
    change = ApiChange(
        type=ChangeType.REQUEST_SCHEMA_CHANGED,
        severity=Severity.BREAKING,
        old_value=["x" * 50 for _ in range(50)],
    )
    rendered = change.value_lines()[0]
    assert rendered.endswith("... (truncated)")
    assert len(rendered) < MAX_VALUE_CHARS + 40
