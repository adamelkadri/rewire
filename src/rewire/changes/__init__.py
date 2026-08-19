"""API specification diffing and breaking-change classification.

Deterministic: no LLM is involved at any point. Comparing two specifications is
a decidable problem, and treating it as one keeps the result reproducible,
instant and testable against known-correct fixtures.

    >>> from rewire.changes import diff_specs, load_spec
    >>> report = diff_specs(load_spec("old.yaml"), load_spec("new.yaml"))
    >>> report.summary.breaking
    3
"""

from rewire.changes.differ import diff_specs
from rewire.changes.models import (
    ApiChange,
    ChangeLocation,
    ChangeReport,
    ChangeSummary,
    ChangeType,
    Severity,
    SpecRef,
)
from rewire.changes.renames import RenameCandidate, detect_renames
from rewire.changes.schema_diff import (
    SchemaChange,
    SchemaChangeKind,
    SchemaDirection,
    diff_schema,
    severity_for,
)
from rewire.changes.spec import ApiSpec, Operation, Parameter, load_spec, parse_spec_text

__all__ = [
    "ApiChange",
    "ApiSpec",
    "ChangeLocation",
    "ChangeReport",
    "ChangeSummary",
    "ChangeType",
    "Operation",
    "Parameter",
    "RenameCandidate",
    "SchemaChange",
    "SchemaChangeKind",
    "SchemaDirection",
    "Severity",
    "SpecRef",
    "detect_renames",
    "diff_schema",
    "diff_specs",
    "load_spec",
    "parse_spec_text",
    "severity_for",
]
