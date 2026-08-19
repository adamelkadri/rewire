"""Structural JSON Schema comparison with request/response variance.

The central idea here is that *the same structural edit is breaking in one
direction and harmless in the other*, because a client is a producer of requests
and a consumer of responses:

===========================  ==================  ==================
Edit                         In a request        In a response
===========================  ==================  ==================
Field removed                breaking            breaking
Field added                  non-breaking        non-breaking
Field became required        breaking            non-breaking
Field became optional        non-breaking        breaking
Enum value removed           breaking            non-breaking
Enum value added             non-breaking        potentially breaking
Constraint tightened         potentially         non-breaking
===========================  ==================  ==================

"Field became optional" is the case most tools get wrong. On a response it means
a field the client has always been able to read may now be absent — every
unguarded access to it is a latent ``KeyError``. Classifying that as non-breaking
because "nothing was removed" is exactly the false negative this module exists to
avoid.

This module reports *what* changed; :mod:`rewire.changes.differ` decides which
:class:`~rewire.changes.models.ChangeType` each finding maps to.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from rewire.changes.models import Severity
from rewire.changes.spec import CIRCULAR_REF_KEY, JsonSchema

#: Depth at which recursion stops. Cycles already resolve to opaque markers, so
#: this only guards against pathologically deep but finite documents.
MAX_SCHEMA_DEPTH: Final[int] = 40

#: Validation keywords compared for equality but not recursed into. Tightening
#: any of them can reject a payload that previously succeeded.
CONSTRAINT_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)

#: Composition keywords. Deciding whether two ``oneOf`` branches are compatible
#: is a subtyping problem; Rewire reports the change and declines to guess.
COMPOSITION_KEYWORDS: Final[frozenset[str]] = frozenset({"oneOf", "anyOf", "allOf", "not"})


class SchemaDirection(StrEnum):
    """Which way data flows through the schema being compared."""

    REQUEST = "request"
    RESPONSE = "response"


class SchemaChangeKind(StrEnum):
    """The structural edit that was detected."""

    FIELD_ADDED = "field_added"
    FIELD_REMOVED = "field_removed"
    TYPE_CHANGED = "type_changed"
    BECAME_REQUIRED = "became_required"
    BECAME_OPTIONAL = "became_optional"
    ENUM_VALUES_ADDED = "enum_values_added"
    ENUM_VALUES_REMOVED = "enum_values_removed"
    FORMAT_CHANGED = "format_changed"
    CONSTRAINT_CHANGED = "constraint_changed"
    STRUCTURE_CHANGED = "structure_changed"
    DEPRECATED = "deprecated"


class SchemaChange(BaseModel):
    """One structural difference between two schemas."""

    model_config = ConfigDict(frozen=True)

    kind: SchemaChangeKind
    #: Dotted path from the schema root, e.g. ``messages[].content``. Empty at
    #: the root itself.
    path: str = ""
    old: Any = None
    new: Any = None

    @property
    def leaf(self) -> str | None:
        """Final path segment — the name a migration would search the code for."""
        if not self.path:
            return None
        return self.path.rsplit(".", maxsplit=1)[-1].removesuffix("[]")

    @property
    def is_root(self) -> bool:
        """Whether the change applies to the schema as a whole."""
        return not self.path


#: Severity of each structural edit, per direction. Encoded as data rather than
#: branching logic so that the variance table is inspectable and testable
#: directly -- ``test_schema_diff`` asserts every combination is covered.
SEVERITY_TABLE: Final[dict[tuple[SchemaDirection, SchemaChangeKind], Severity]] = {
    # --- requests: the client produces this payload -------------------------
    (SchemaDirection.REQUEST, SchemaChangeKind.FIELD_ADDED): Severity.NON_BREAKING,
    (SchemaDirection.REQUEST, SchemaChangeKind.FIELD_REMOVED): Severity.BREAKING,
    (SchemaDirection.REQUEST, SchemaChangeKind.TYPE_CHANGED): Severity.BREAKING,
    (SchemaDirection.REQUEST, SchemaChangeKind.BECAME_REQUIRED): Severity.BREAKING,
    (SchemaDirection.REQUEST, SchemaChangeKind.BECAME_OPTIONAL): Severity.NON_BREAKING,
    (SchemaDirection.REQUEST, SchemaChangeKind.ENUM_VALUES_ADDED): Severity.NON_BREAKING,
    (SchemaDirection.REQUEST, SchemaChangeKind.ENUM_VALUES_REMOVED): Severity.BREAKING,
    (SchemaDirection.REQUEST, SchemaChangeKind.FORMAT_CHANGED): Severity.POTENTIALLY_BREAKING,
    (SchemaDirection.REQUEST, SchemaChangeKind.CONSTRAINT_CHANGED): Severity.POTENTIALLY_BREAKING,
    (SchemaDirection.REQUEST, SchemaChangeKind.STRUCTURE_CHANGED): Severity.POTENTIALLY_BREAKING,
    (SchemaDirection.REQUEST, SchemaChangeKind.DEPRECATED): Severity.POTENTIALLY_BREAKING,
    # --- responses: the client consumes this payload ------------------------
    (SchemaDirection.RESPONSE, SchemaChangeKind.FIELD_ADDED): Severity.NON_BREAKING,
    (SchemaDirection.RESPONSE, SchemaChangeKind.FIELD_REMOVED): Severity.BREAKING,
    (SchemaDirection.RESPONSE, SchemaChangeKind.TYPE_CHANGED): Severity.BREAKING,
    (SchemaDirection.RESPONSE, SchemaChangeKind.BECAME_REQUIRED): Severity.NON_BREAKING,
    (SchemaDirection.RESPONSE, SchemaChangeKind.BECAME_OPTIONAL): Severity.BREAKING,
    (SchemaDirection.RESPONSE, SchemaChangeKind.ENUM_VALUES_ADDED): Severity.POTENTIALLY_BREAKING,
    (SchemaDirection.RESPONSE, SchemaChangeKind.ENUM_VALUES_REMOVED): Severity.NON_BREAKING,
    (SchemaDirection.RESPONSE, SchemaChangeKind.FORMAT_CHANGED): Severity.POTENTIALLY_BREAKING,
    (SchemaDirection.RESPONSE, SchemaChangeKind.CONSTRAINT_CHANGED): Severity.NON_BREAKING,
    (SchemaDirection.RESPONSE, SchemaChangeKind.STRUCTURE_CHANGED): Severity.POTENTIALLY_BREAKING,
    (SchemaDirection.RESPONSE, SchemaChangeKind.DEPRECATED): Severity.POTENTIALLY_BREAKING,
}


def severity_for(direction: SchemaDirection, kind: SchemaChangeKind) -> Severity:
    """Return the severity of ``kind`` when it occurs in ``direction``."""
    return SEVERITY_TABLE[(direction, kind)]


def type_set(schema: JsonSchema) -> frozenset[str]:
    """Return a schema's declared types, normalised across OpenAPI 3.0 and 3.1.

    3.0 spells a nullable string ``{"type": "string", "nullable": true}``; 3.1
    spells it ``{"type": ["string", "null"]}``. Both mean the same thing, and a
    document upgraded from one to the other must not report a type change.
    """
    declared = schema.get("type")
    if isinstance(declared, str):
        types = {declared}
    elif isinstance(declared, list):
        types = {str(item) for item in declared}
    else:
        types = set()

    if schema.get("nullable") is True:
        types.add("null")
    return frozenset(types)


def _join(prefix: str, segment: str) -> str:
    return f"{prefix}.{segment}" if prefix else segment


def _required_names(schema: JsonSchema) -> set[str]:
    required = schema.get("required")
    return {str(name) for name in required} if isinstance(required, list) else set()


def _properties(schema: JsonSchema) -> dict[str, JsonSchema]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {str(k): v for k, v in properties.items() if isinstance(v, dict)}


def _enum_values(schema: JsonSchema) -> list[Any] | None:
    enum = schema.get("enum")
    return list(enum) if isinstance(enum, list) else None


def _hashable(value: Any) -> Any:
    """Make an arbitrary JSON value usable in a set, preserving distinctness."""
    if isinstance(value, list):
        return ("__list__", tuple(_hashable(item) for item in value))
    if isinstance(value, dict):
        return ("__dict__", tuple(sorted((k, _hashable(v)) for k, v in value.items())))
    return value


def diff_schema(
    old: JsonSchema | None,
    new: JsonSchema | None,
    *,
    path: str = "",
    depth: int = 0,
) -> list[SchemaChange]:
    """Compare two JSON Schemas and return their structural differences.

    The result is direction-agnostic: it says what changed, not how bad it is.
    Use :func:`severity_for` to grade a finding once the direction is known.

    Args:
        old: Schema from the previous specification, or ``None`` if absent.
        new: Schema from the new specification, or ``None`` if absent.
        path: Dotted path of the schema being compared, used to build the paths
            reported on nested findings.
        depth: Current recursion depth; recursion stops at
            :data:`MAX_SCHEMA_DEPTH`.
    """
    if old is None and new is None:
        return []
    if old is None or new is None:
        kind = SchemaChangeKind.FIELD_ADDED if old is None else SchemaChangeKind.FIELD_REMOVED
        return [SchemaChange(kind=kind, path=path, old=old, new=new)]
    if depth >= MAX_SCHEMA_DEPTH:
        return []

    changes: list[SchemaChange] = []
    changes.extend(_diff_circular_markers(old, new, path=path))
    if changes:
        return changes

    changes.extend(_diff_types(old, new, path=path))
    changes.extend(_diff_enum(old, new, path=path))
    changes.extend(_diff_format(old, new, path=path))
    changes.extend(_diff_constraints(old, new, path=path))
    changes.extend(_diff_composition(old, new, path=path))
    changes.extend(_diff_deprecation(old, new, path=path))
    changes.extend(_diff_properties(old, new, path=path, depth=depth))
    changes.extend(_diff_items(old, new, path=path, depth=depth))
    changes.extend(_diff_additional_properties(old, new, path=path))
    return changes


def _diff_circular_markers(old: JsonSchema, new: JsonSchema, *, path: str) -> list[SchemaChange]:
    """Compare cycle markers by target; their contents are unresolvable by design."""
    old_ref, new_ref = old.get(CIRCULAR_REF_KEY), new.get(CIRCULAR_REF_KEY)
    if old_ref is None and new_ref is None:
        return []
    if old_ref == new_ref:
        return []
    return [
        SchemaChange(kind=SchemaChangeKind.STRUCTURE_CHANGED, path=path, old=old_ref, new=new_ref)
    ]


def _diff_types(old: JsonSchema, new: JsonSchema, *, path: str) -> list[SchemaChange]:
    old_types, new_types = type_set(old), type_set(new)
    if old_types == new_types:
        return []
    return [
        SchemaChange(
            kind=SchemaChangeKind.TYPE_CHANGED,
            path=path,
            old=sorted(old_types) or None,
            new=sorted(new_types) or None,
        )
    ]


def _diff_enum(old: JsonSchema, new: JsonSchema, *, path: str) -> list[SchemaChange]:
    old_enum, new_enum = _enum_values(old), _enum_values(new)
    if old_enum is None and new_enum is None:
        return []
    if old_enum is None or new_enum is None:
        # Gaining or losing an enum constraint entirely is a structural change,
        # not a value-set change.
        return [
            SchemaChange(
                kind=SchemaChangeKind.STRUCTURE_CHANGED, path=path, old=old_enum, new=new_enum
            )
        ]

    old_set = {_hashable(value) for value in old_enum}
    new_set = {_hashable(value) for value in new_enum}
    changes: list[SchemaChange] = []
    if removed := [value for value in old_enum if _hashable(value) not in new_set]:
        changes.append(
            SchemaChange(kind=SchemaChangeKind.ENUM_VALUES_REMOVED, path=path, old=removed)
        )
    if added := [value for value in new_enum if _hashable(value) not in old_set]:
        changes.append(SchemaChange(kind=SchemaChangeKind.ENUM_VALUES_ADDED, path=path, new=added))
    return changes


def _diff_format(old: JsonSchema, new: JsonSchema, *, path: str) -> list[SchemaChange]:
    if old.get("format") == new.get("format"):
        return []
    return [
        SchemaChange(
            kind=SchemaChangeKind.FORMAT_CHANGED,
            path=path,
            old=old.get("format"),
            new=new.get("format"),
        )
    ]


def _diff_constraints(old: JsonSchema, new: JsonSchema, *, path: str) -> list[SchemaChange]:
    return [
        SchemaChange(
            kind=SchemaChangeKind.CONSTRAINT_CHANGED,
            path=path,
            old={keyword: old[keyword]} if keyword in old else None,
            new={keyword: new[keyword]} if keyword in new else None,
        )
        for keyword in sorted(CONSTRAINT_KEYWORDS)
        if old.get(keyword) != new.get(keyword)
    ]


def _diff_composition(old: JsonSchema, new: JsonSchema, *, path: str) -> list[SchemaChange]:
    changed = [
        keyword for keyword in sorted(COMPOSITION_KEYWORDS) if old.get(keyword) != new.get(keyword)
    ]
    if not changed:
        return []
    return [
        SchemaChange(
            kind=SchemaChangeKind.STRUCTURE_CHANGED,
            path=path,
            old={keyword: old[keyword] for keyword in changed if keyword in old} or None,
            new={keyword: new[keyword] for keyword in changed if keyword in new} or None,
        )
    ]


def _diff_deprecation(old: JsonSchema, new: JsonSchema, *, path: str) -> list[SchemaChange]:
    if old.get("deprecated") is not True and new.get("deprecated") is True:
        return [SchemaChange(kind=SchemaChangeKind.DEPRECATED, path=path, old=False, new=True)]
    return []


def _diff_properties(
    old: JsonSchema, new: JsonSchema, *, path: str, depth: int
) -> list[SchemaChange]:
    old_props, new_props = _properties(old), _properties(new)
    old_required, new_required = _required_names(old), _required_names(new)
    changes: list[SchemaChange] = []

    for name in sorted(set(old_props) - set(new_props)):
        changes.append(
            SchemaChange(
                kind=SchemaChangeKind.FIELD_REMOVED, path=_join(path, name), old=old_props[name]
            )
        )
    for name in sorted(set(new_props) - set(old_props)):
        changes.append(
            SchemaChange(
                kind=SchemaChangeKind.FIELD_ADDED, path=_join(path, name), new=new_props[name]
            )
        )

    for name in sorted(set(old_props) & set(new_props)):
        child_path = _join(path, name)
        was_required, is_required = name in old_required, name in new_required
        if not was_required and is_required:
            changes.append(
                SchemaChange(
                    kind=SchemaChangeKind.BECAME_REQUIRED, path=child_path, old=False, new=True
                )
            )
        elif was_required and not is_required:
            changes.append(
                SchemaChange(
                    kind=SchemaChangeKind.BECAME_OPTIONAL, path=child_path, old=True, new=False
                )
            )
        changes.extend(
            diff_schema(old_props[name], new_props[name], path=child_path, depth=depth + 1)
        )

    # A field listed in 'required' without a matching 'properties' entry is legal
    # and still meaningful, so requirement changes are tracked for those too.
    for name in sorted((new_required - old_required) - set(new_props)):
        changes.append(
            SchemaChange(
                kind=SchemaChangeKind.BECAME_REQUIRED, path=_join(path, name), old=False, new=True
            )
        )
    for name in sorted((old_required - new_required) - set(old_props)):
        changes.append(
            SchemaChange(
                kind=SchemaChangeKind.BECAME_OPTIONAL, path=_join(path, name), old=True, new=False
            )
        )
    return changes


def _diff_items(old: JsonSchema, new: JsonSchema, *, path: str, depth: int) -> list[SchemaChange]:
    old_items, new_items = old.get("items"), new.get("items")
    if not isinstance(old_items, dict) or not isinstance(new_items, dict):
        if old_items != new_items:
            return [
                SchemaChange(
                    kind=SchemaChangeKind.STRUCTURE_CHANGED, path=path, old=old_items, new=new_items
                )
            ]
        return []
    return diff_schema(old_items, new_items, path=f"{path}[]" if path else "[]", depth=depth + 1)


def _diff_additional_properties(
    old: JsonSchema, new: JsonSchema, *, path: str
) -> list[SchemaChange]:
    if old.get("additionalProperties") == new.get("additionalProperties"):
        return []
    return [
        SchemaChange(
            kind=SchemaChangeKind.STRUCTURE_CHANGED,
            path=path,
            old=old.get("additionalProperties"),
            new=new.get("additionalProperties"),
        )
    ]


__all__ = [
    "COMPOSITION_KEYWORDS",
    "CONSTRAINT_KEYWORDS",
    "MAX_SCHEMA_DEPTH",
    "SEVERITY_TABLE",
    "SchemaChange",
    "SchemaChangeKind",
    "SchemaDirection",
    "diff_schema",
    "severity_for",
    "type_set",
]
