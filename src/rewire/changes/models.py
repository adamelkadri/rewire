"""Typed models for detected API changes.

These are the contract between deterministic detection (Phase 1) and everything
downstream — impact analysis, the agent, evaluation. They are deliberately
machine-first: a change carries the endpoint, the field and the severity as
structured data, not as a sentence a later stage would have to parse back.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Severity(StrEnum):
    """How much downstream code a change is expected to break."""

    BREAKING = "breaking"
    POTENTIALLY_BREAKING = "potentially_breaking"
    NON_BREAKING = "non_breaking"

    @property
    def rank(self) -> int:
        """Sort key: most severe first."""
        return _SEVERITY_RANK[self]

    def at_least(self, threshold: Severity) -> bool:
        """Whether this severity is as severe as ``threshold`` or worse."""
        return self.rank <= threshold.rank


_SEVERITY_RANK: Final[dict[Severity, int]] = {
    Severity.BREAKING: 0,
    Severity.POTENTIALLY_BREAKING: 1,
    Severity.NON_BREAKING: 2,
}


class ChangeType(StrEnum):
    """The specific edit that was detected between two specifications."""

    # --- operation level ----------------------------------------------------
    ENDPOINT_ADDED = "endpoint_added"
    ENDPOINT_REMOVED = "endpoint_removed"
    OPERATION_ADDED = "operation_added"
    OPERATION_REMOVED = "operation_removed"
    OPERATION_DEPRECATED = "operation_deprecated"

    # --- parameters ---------------------------------------------------------
    PARAMETER_ADDED = "parameter_added"
    PARAMETER_REMOVED = "parameter_removed"
    REQUIRED_PARAMETER_ADDED = "required_parameter_added"
    PARAMETER_BECAME_REQUIRED = "parameter_became_required"
    PARAMETER_BECAME_OPTIONAL = "parameter_became_optional"
    PARAMETER_TYPE_CHANGED = "parameter_type_changed"
    PARAMETER_SCHEMA_CHANGED = "parameter_schema_changed"
    PARAMETER_DEPRECATED = "parameter_deprecated"

    # --- request body -------------------------------------------------------
    REQUEST_BODY_ADDED = "request_body_added"
    REQUEST_BODY_REMOVED = "request_body_removed"
    REQUEST_BODY_BECAME_REQUIRED = "request_body_became_required"
    REQUEST_BODY_BECAME_OPTIONAL = "request_body_became_optional"
    REQUEST_CONTENT_TYPE_ADDED = "request_content_type_added"
    REQUEST_CONTENT_TYPE_REMOVED = "request_content_type_removed"
    REQUEST_FIELD_ADDED = "request_field_added"
    REQUEST_FIELD_REMOVED = "request_field_removed"
    REQUEST_FIELD_TYPE_CHANGED = "request_field_type_changed"
    REQUEST_FIELD_BECAME_REQUIRED = "request_field_became_required"
    REQUEST_FIELD_BECAME_OPTIONAL = "request_field_became_optional"
    REQUEST_SCHEMA_CHANGED = "request_schema_changed"

    # --- responses ----------------------------------------------------------
    RESPONSE_ADDED = "response_added"
    RESPONSE_REMOVED = "response_removed"
    RESPONSE_CONTENT_TYPE_ADDED = "response_content_type_added"
    RESPONSE_CONTENT_TYPE_REMOVED = "response_content_type_removed"
    RESPONSE_FIELD_ADDED = "response_field_added"
    RESPONSE_FIELD_REMOVED = "response_field_removed"
    RESPONSE_FIELD_TYPE_CHANGED = "response_field_type_changed"
    RESPONSE_FIELD_BECAME_REQUIRED = "response_field_became_required"
    RESPONSE_FIELD_BECAME_OPTIONAL = "response_field_became_optional"
    RESPONSE_SCHEMA_CHANGED = "response_schema_changed"


class ChangeLocation(StrEnum):
    """Which part of an operation a change applies to."""

    OPERATION = "operation"
    QUERY = "query"
    HEADER = "header"
    PATH = "path"
    COOKIE = "cookie"
    REQUEST_BODY = "request_body"
    RESPONSE = "response"


class ApiChange(BaseModel):
    """A single detected difference between two API specifications.

    Only ``type`` and ``severity`` are always present. The remaining fields
    locate the change precisely enough for Phase 3 to search a repository for it,
    and are omitted from serialised output when they do not apply.
    """

    model_config = ConfigDict(frozen=True)

    type: ChangeType
    severity: Severity
    #: ``"POST /v1/messages"``. Absent only for specification-wide changes.
    endpoint: str | None = None
    path: str | None = None
    method: str | None = None
    location: ChangeLocation | None = None
    #: Leaf name of the affected field — what a code search would look for.
    field: str | None = None
    #: Full dotted path to the field, e.g. ``messages[].content``.
    field_path: str | None = None
    #: Name that replaces ``field``, when a rename was detected.
    replacement: str | None = None
    status_code: str | None = None
    content_type: str | None = None
    old_value: Any = None
    new_value: Any = None
    #: One-line human-readable summary. Derived, never parsed.
    detail: str = ""

    @model_validator(mode="after")
    def _default_field_from_path(self) -> Self:
        if self.field is None and self.field_path:
            leaf = self.field_path.rsplit(".", maxsplit=1)[-1].removesuffix("[]")
            object.__setattr__(self, "field", leaf or None)
        return self

    @property
    def is_breaking(self) -> bool:
        """Whether this change definitely breaks existing client code."""
        return self.severity is Severity.BREAKING

    @property
    def sort_key(self) -> tuple[int, str, str, str]:
        """Deterministic ordering: severity, then endpoint, type and field."""
        return (self.severity.rank, self.endpoint or "", self.type.value, self.field_path or "")


class ChangeSummary(BaseModel):
    """Aggregate counts over a set of changes."""

    model_config = ConfigDict(frozen=True)

    total: int = 0
    breaking: int = 0
    potentially_breaking: int = 0
    non_breaking: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    endpoints_affected: int = 0

    @classmethod
    def from_changes(cls, changes: list[ApiChange]) -> ChangeSummary:
        """Compute a summary from a list of changes."""
        severities = Counter(change.severity for change in changes)
        return cls(
            total=len(changes),
            breaking=severities[Severity.BREAKING],
            potentially_breaking=severities[Severity.POTENTIALLY_BREAKING],
            non_breaking=severities[Severity.NON_BREAKING],
            by_type=dict(sorted(Counter(change.type.value for change in changes).items())),
            endpoints_affected=len({c.endpoint for c in changes if c.endpoint is not None}),
        )


class SpecRef(BaseModel):
    """Identifying information about one side of a comparison."""

    model_config = ConfigDict(frozen=True)

    title: str | None = None
    version: str | None = None
    openapi_version: str | None = None
    source: str | None = None


class ChangeReport(BaseModel):
    """The complete, deterministic result of comparing two specifications."""

    model_config = ConfigDict(frozen=True)

    old_spec: SpecRef = Field(default_factory=SpecRef)
    new_spec: SpecRef = Field(default_factory=SpecRef)
    changes: list[ApiChange] = Field(default_factory=list)
    summary: ChangeSummary = Field(default_factory=ChangeSummary)

    @classmethod
    def build(cls, old: SpecRef, new: SpecRef, changes: list[ApiChange]) -> ChangeReport:
        """Assemble a report, sorting changes and computing the summary."""
        ordered = sorted(changes, key=lambda change: change.sort_key)
        return cls(
            old_spec=old,
            new_spec=new,
            changes=ordered,
            summary=ChangeSummary.from_changes(ordered),
        )

    @property
    def has_breaking_changes(self) -> bool:
        """Whether any change is definitely breaking."""
        return self.summary.breaking > 0

    def filter(self, minimum: Severity) -> list[ApiChange]:
        """Return changes at least as severe as ``minimum``."""
        return [change for change in self.changes if change.severity.at_least(minimum)]

    def for_endpoint(self, endpoint: str) -> list[ApiChange]:
        """Return every change affecting ``endpoint``."""
        return [change for change in self.changes if change.endpoint == endpoint]


__all__ = [
    "ApiChange",
    "ChangeLocation",
    "ChangeReport",
    "ChangeSummary",
    "ChangeType",
    "Severity",
    "SpecRef",
]
