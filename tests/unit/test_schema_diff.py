"""Tests for structural schema comparison and the request/response variance table."""

from __future__ import annotations

import pytest

from rewire.changes.models import Severity
from rewire.changes.schema_diff import (
    MAX_SCHEMA_DEPTH,
    SEVERITY_TABLE,
    SchemaChangeKind,
    SchemaDirection,
    diff_schema,
    severity_for,
    type_set,
)
from rewire.changes.spec import CIRCULAR_REF_KEY


def kinds(old: dict, new: dict) -> list[SchemaChangeKind]:
    return [change.kind for change in diff_schema(old, new)]


def only(old: dict, new: dict) -> tuple[SchemaChangeKind, str]:
    changes = diff_schema(old, new)
    assert len(changes) == 1, changes
    return changes[0].kind, changes[0].path


# ------------------------------------------------------------- variance ----


def test_variance_table_covers_every_combination() -> None:
    """A missing entry would raise KeyError at diff time, i.e. in production."""
    expected = {(direction, kind) for direction in SchemaDirection for kind in SchemaChangeKind}
    assert set(SEVERITY_TABLE) == expected


@pytest.mark.parametrize(
    ("kind", "in_request", "in_response"),
    [
        # The asymmetric cases are the point of the table.
        (SchemaChangeKind.BECAME_REQUIRED, Severity.BREAKING, Severity.NON_BREAKING),
        (SchemaChangeKind.BECAME_OPTIONAL, Severity.NON_BREAKING, Severity.BREAKING),
        (SchemaChangeKind.ENUM_VALUES_ADDED, Severity.NON_BREAKING, Severity.POTENTIALLY_BREAKING),
        (SchemaChangeKind.ENUM_VALUES_REMOVED, Severity.BREAKING, Severity.NON_BREAKING),
        (
            SchemaChangeKind.CONSTRAINT_CHANGED,
            Severity.POTENTIALLY_BREAKING,
            Severity.NON_BREAKING,
        ),
        # The symmetric ones must stay symmetric.
        (SchemaChangeKind.FIELD_REMOVED, Severity.BREAKING, Severity.BREAKING),
        (SchemaChangeKind.FIELD_ADDED, Severity.NON_BREAKING, Severity.NON_BREAKING),
        (SchemaChangeKind.TYPE_CHANGED, Severity.BREAKING, Severity.BREAKING),
    ],
)
def test_severity_depends_on_direction(
    kind: SchemaChangeKind, in_request: Severity, in_response: Severity
) -> None:
    assert severity_for(SchemaDirection.REQUEST, kind) is in_request
    assert severity_for(SchemaDirection.RESPONSE, kind) is in_response


# ----------------------------------------------------------------- types ----


def test_identical_schemas_produce_no_changes() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    assert diff_schema(schema, dict(schema)) == []


def test_type_change_at_root() -> None:
    assert only({"type": "string"}, {"type": "integer"}) == (SchemaChangeKind.TYPE_CHANGED, "")


def test_nullable_30_equals_union_null_31() -> None:
    """A 3.0-to-3.1 syntax upgrade must not read as a type change."""
    assert diff_schema({"type": "string", "nullable": True}, {"type": ["string", "null"]}) == []


def test_becoming_nullable_is_a_type_change() -> None:
    kind, _ = only({"type": "string"}, {"type": "string", "nullable": True})
    assert kind is SchemaChangeKind.TYPE_CHANGED


def test_type_set_normalisation() -> None:
    assert type_set({"type": "string"}) == frozenset({"string"})
    assert type_set({"type": ["string", "null"]}) == frozenset({"string", "null"})
    assert type_set({"type": "string", "nullable": True}) == frozenset({"string", "null"})
    assert type_set({}) == frozenset()


# ------------------------------------------------------------ properties ----


def test_field_added_and_removed() -> None:
    old = {"type": "object", "properties": {"a": {"type": "string"}}}
    new = {"type": "object", "properties": {"b": {"type": "string"}}}
    assert kinds(old, new) == [SchemaChangeKind.FIELD_REMOVED, SchemaChangeKind.FIELD_ADDED]


def test_nested_field_paths_are_dotted() -> None:
    old = {"type": "object", "properties": {"outer": {"type": "object", "properties": {}}}}
    new = {
        "type": "object",
        "properties": {"outer": {"type": "object", "properties": {"inner": {"type": "string"}}}},
    }
    assert only(old, new) == (SchemaChangeKind.FIELD_ADDED, "outer.inner")


def test_array_item_paths_use_bracket_notation() -> None:
    old = {"type": "array", "items": {"type": "object", "properties": {"a": {"type": "string"}}}}
    new = {"type": "array", "items": {"type": "object", "properties": {}}}
    assert only(old, new) == (SchemaChangeKind.FIELD_REMOVED, "[].a")


def test_leaf_strips_array_marker() -> None:
    old = {"type": "object", "properties": {"xs": {"type": "array", "items": {"type": "string"}}}}
    new = {"type": "object", "properties": {"xs": {"type": "array", "items": {"type": "integer"}}}}
    change = diff_schema(old, new)[0]
    assert change.path == "xs[]"
    assert change.leaf == "xs"


def test_requirement_transitions() -> None:
    base = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert kinds(base, {**base, "required": ["a"]}) == [SchemaChangeKind.BECAME_REQUIRED]
    assert kinds({**base, "required": ["a"]}, base) == [SchemaChangeKind.BECAME_OPTIONAL]


def test_requirement_tracked_without_a_properties_entry() -> None:
    """'required' may name a field that 'properties' does not describe."""
    assert kinds({"type": "object"}, {"type": "object", "required": ["a"]}) == [
        SchemaChangeKind.BECAME_REQUIRED
    ]


def test_root_change_is_flagged_as_root() -> None:
    assert diff_schema({"type": "string"}, {"type": "integer"})[0].is_root
    change = diff_schema(
        {"type": "object", "properties": {"a": {"type": "string"}}},
        {"type": "object", "properties": {"a": {"type": "integer"}}},
    )[0]
    assert not change.is_root
    assert change.leaf == "a"


# ------------------------------------------------------------------ enums ----


def test_enum_values_added_and_removed_separately() -> None:
    changes = diff_schema({"enum": ["a", "b"]}, {"enum": ["b", "c"]})
    by_kind = {change.kind: change for change in changes}
    assert by_kind[SchemaChangeKind.ENUM_VALUES_REMOVED].old == ["a"]
    assert by_kind[SchemaChangeKind.ENUM_VALUES_ADDED].new == ["c"]


def test_enum_reordering_is_not_a_change() -> None:
    assert diff_schema({"enum": ["a", "b"]}, {"enum": ["b", "a"]}) == []


def test_gaining_an_enum_constraint_is_structural() -> None:
    assert kinds({"type": "string"}, {"type": "string", "enum": ["a"]}) == [
        SchemaChangeKind.STRUCTURE_CHANGED
    ]


def test_unhashable_enum_values_are_supported() -> None:
    """Enum entries may be objects or arrays, which cannot go in a set unaided."""
    changes = diff_schema({"enum": [{"a": 1}, [1, 2]]}, {"enum": [{"a": 1}]})
    assert [change.kind for change in changes] == [SchemaChangeKind.ENUM_VALUES_REMOVED]
    assert changes[0].old == [[1, 2]]


# ------------------------------------------------------------ constraints ----


def test_constraint_change_is_reported_per_keyword() -> None:
    changes = diff_schema(
        {"type": "integer", "minimum": 0, "maximum": 100},
        {"type": "integer", "minimum": 1, "maximum": 50},
    )
    assert [change.kind for change in changes] == [SchemaChangeKind.CONSTRAINT_CHANGED] * 2
    assert {next(iter(change.new or {})) for change in changes} == {"minimum", "maximum"}


def test_format_change_is_distinct_from_type_change() -> None:
    assert only(
        {"type": "string", "format": "date"}, {"type": "string", "format": "date-time"}
    ) == (
        SchemaChangeKind.FORMAT_CHANGED,
        "",
    )


def test_composition_change_is_structural() -> None:
    assert kinds(
        {"oneOf": [{"type": "string"}]}, {"oneOf": [{"type": "string"}, {"type": "integer"}]}
    ) == [SchemaChangeKind.STRUCTURE_CHANGED]


def test_additional_properties_change_is_structural() -> None:
    assert kinds({"type": "object"}, {"type": "object", "additionalProperties": False}) == [
        SchemaChangeKind.STRUCTURE_CHANGED
    ]


def test_deprecation_is_reported_once() -> None:
    assert kinds({"type": "string"}, {"type": "string", "deprecated": True}) == [
        SchemaChangeKind.DEPRECATED
    ]
    assert diff_schema({"type": "string", "deprecated": True}, {"type": "string"}) == []


# ------------------------------------------------------------------ edges ----


def test_absent_schema_on_one_side() -> None:
    assert diff_schema(None, {"type": "string"})[0].kind is SchemaChangeKind.FIELD_ADDED
    assert diff_schema({"type": "string"}, None)[0].kind is SchemaChangeKind.FIELD_REMOVED
    assert diff_schema(None, None) == []


def test_circular_markers_compare_by_target() -> None:
    same = {CIRCULAR_REF_KEY: "#/components/schemas/Node"}
    other = {CIRCULAR_REF_KEY: "#/components/schemas/Leaf"}
    assert diff_schema(same, dict(same)) == []
    assert kinds(same, other) == [SchemaChangeKind.STRUCTURE_CHANGED]


def test_circular_marker_short_circuits_other_comparisons() -> None:
    """A cycle marker is opaque; comparing its neighbours would be meaningless."""
    old = {CIRCULAR_REF_KEY: "#/a", "type": "object"}
    new = {CIRCULAR_REF_KEY: "#/b", "type": "string"}
    assert kinds(old, new) == [SchemaChangeKind.STRUCTURE_CHANGED]


def test_recursion_stops_at_the_depth_limit() -> None:
    def nest(depth: int, leaf: dict) -> dict:
        schema = leaf
        for _ in range(depth):
            schema = {"type": "object", "properties": {"n": schema}}
        return schema

    deep_old = nest(MAX_SCHEMA_DEPTH + 10, {"type": "string"})
    deep_new = nest(MAX_SCHEMA_DEPTH + 10, {"type": "integer"})
    assert diff_schema(deep_old, deep_new) == []  # beyond the limit, not a crash

    shallow_old = nest(3, {"type": "string"})
    shallow_new = nest(3, {"type": "integer"})
    assert len(diff_schema(shallow_old, shallow_new)) == 1


def test_items_mismatch_between_object_and_scalar() -> None:
    assert kinds(
        {"type": "array", "items": True}, {"type": "array", "items": {"type": "string"}}
    ) == [SchemaChangeKind.STRUCTURE_CHANGED]


def test_non_dict_property_values_are_ignored() -> None:
    """Malformed sub-schemas must not crash the differ."""
    old = {"type": "object", "properties": {"a": "not-a-schema", "b": {"type": "string"}}}
    new = {"type": "object", "properties": {"a": "not-a-schema", "b": {"type": "integer"}}}
    assert kinds(old, new) == [SchemaChangeKind.TYPE_CHANGED]


def test_leaf_is_none_at_the_root() -> None:
    change = diff_schema({"type": "string"}, {"type": "integer"})[0]
    assert change.leaf is None


def test_requirement_dropped_without_a_properties_entry() -> None:
    """'required' can name a field the old schema never described."""
    assert kinds({"type": "object", "required": ["ghost"]}, {"type": "object"}) == [
        SchemaChangeKind.BECAME_OPTIONAL
    ]
