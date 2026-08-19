"""Deterministic comparison of two OpenAPI specifications.

This is the orchestrator for Phase 1. It walks two normalised
:class:`~rewire.changes.spec.ApiSpec` documents in parallel and emits typed
:class:`~rewire.changes.models.ApiChange` records. There is no LLM involved and
no randomness: the same pair of documents always produces byte-identical output.

Severity is never decided here. Structural findings are graded by the variance
table in :mod:`rewire.changes.schema_diff`, which knows that removing a response
field and removing a request field break client code for different reasons.

Renames are reported as a linked pair rather than a third kind of event: the
removal carries ``replacement``, and the matching addition is upgraded from
non-breaking to potentially breaking because it is now part of a migration
rather than an unrelated new option.
"""

from __future__ import annotations

from rewire.changes.models import (
    ApiChange,
    ChangeLocation,
    ChangeReport,
    ChangeType,
    Severity,
    SpecRef,
)
from rewire.changes.renames import RENAME_SCORE_THRESHOLD, detect_renames
from rewire.changes.schema_diff import (
    SchemaChange,
    SchemaChangeKind,
    SchemaDirection,
    diff_schema,
    severity_for,
)
from rewire.changes.spec import (
    ApiSpec,
    Body,
    JsonSchema,
    Operation,
    Parameter,
    ParameterLocation,
    SpecMetadata,
)

_LOCATION_BY_PARAMETER: dict[ParameterLocation, ChangeLocation] = {
    ParameterLocation.QUERY: ChangeLocation.QUERY,
    ParameterLocation.HEADER: ChangeLocation.HEADER,
    ParameterLocation.PATH: ChangeLocation.PATH,
    ParameterLocation.COOKIE: ChangeLocation.COOKIE,
}

#: Maps a structural finding to a change type, per direction. Anything absent
#: falls back to the schema-level type, which keeps the enum closed while still
#: reporting less common edits (enum, format, constraint, composition).
_REQUEST_CHANGE_TYPES: dict[SchemaChangeKind, ChangeType] = {
    SchemaChangeKind.FIELD_ADDED: ChangeType.REQUEST_FIELD_ADDED,
    SchemaChangeKind.FIELD_REMOVED: ChangeType.REQUEST_FIELD_REMOVED,
    SchemaChangeKind.TYPE_CHANGED: ChangeType.REQUEST_FIELD_TYPE_CHANGED,
    SchemaChangeKind.BECAME_REQUIRED: ChangeType.REQUEST_FIELD_BECAME_REQUIRED,
    SchemaChangeKind.BECAME_OPTIONAL: ChangeType.REQUEST_FIELD_BECAME_OPTIONAL,
}

_RESPONSE_CHANGE_TYPES: dict[SchemaChangeKind, ChangeType] = {
    SchemaChangeKind.FIELD_ADDED: ChangeType.RESPONSE_FIELD_ADDED,
    SchemaChangeKind.FIELD_REMOVED: ChangeType.RESPONSE_FIELD_REMOVED,
    SchemaChangeKind.TYPE_CHANGED: ChangeType.RESPONSE_FIELD_TYPE_CHANGED,
    SchemaChangeKind.BECAME_REQUIRED: ChangeType.RESPONSE_FIELD_BECAME_REQUIRED,
    SchemaChangeKind.BECAME_OPTIONAL: ChangeType.RESPONSE_FIELD_BECAME_OPTIONAL,
}


def diff_specs(
    old: ApiSpec,
    new: ApiSpec,
    *,
    rename_threshold: float = RENAME_SCORE_THRESHOLD,
) -> ChangeReport:
    """Compare two normalised specifications and return a sorted change report.

    Args:
        old: The previous specification.
        new: The new specification.
        rename_threshold: Minimum similarity for a removal/addition pair to be
            linked as a rename. Raise it to be more conservative; set it above
            ``1.0`` to disable rename linking entirely.
    """
    changes: list[ApiChange] = []
    changes.extend(_diff_paths(old, new))
    changes.extend(_diff_shared_operations(old, new, rename_threshold=rename_threshold))
    return ChangeReport.build(_spec_ref(old.metadata), _spec_ref(new.metadata), changes)


def _spec_ref(metadata: SpecMetadata) -> SpecRef:
    return SpecRef(
        title=metadata.title,
        version=metadata.version,
        openapi_version=metadata.openapi_version,
        source=metadata.source,
    )


# ------------------------------------------------------------ path level ---


def _diff_paths(old: ApiSpec, new: ApiSpec) -> list[ApiChange]:
    """Report paths and methods that appeared or disappeared.

    A path losing one of several methods is an operation change; a path losing
    its last method is an endpoint change. Distinguishing them matters because
    only the latter means the URL itself is gone.
    """
    changes: list[ApiChange] = []
    old_paths, new_paths = old.paths, new.paths

    for operation in old.operations.values():
        if operation.path not in new_paths:
            changes.append(
                _operation_change(operation, ChangeType.ENDPOINT_REMOVED, Severity.BREAKING)
            )
        elif operation.method not in new.methods_for(operation.path):
            changes.append(
                _operation_change(operation, ChangeType.OPERATION_REMOVED, Severity.BREAKING)
            )

    for operation in new.operations.values():
        if operation.path not in old_paths:
            changes.append(
                _operation_change(operation, ChangeType.ENDPOINT_ADDED, Severity.NON_BREAKING)
            )
        elif operation.method not in old.methods_for(operation.path):
            changes.append(
                _operation_change(operation, ChangeType.OPERATION_ADDED, Severity.NON_BREAKING)
            )

    return changes


def _operation_change(
    operation: Operation, change_type: ChangeType, severity: Severity
) -> ApiChange:
    verb = change_type.value.replace("_", " ")
    return ApiChange(
        type=change_type,
        severity=severity,
        endpoint=operation.endpoint,
        path=operation.path,
        method=operation.method.value.upper(),
        location=ChangeLocation.OPERATION,
        detail=f"{operation.endpoint}: {verb}",
    )


def _diff_shared_operations(
    old: ApiSpec, new: ApiSpec, *, rename_threshold: float
) -> list[ApiChange]:
    changes: list[ApiChange] = []
    for key in sorted(set(old.operations) & set(new.operations)):
        changes.extend(
            _diff_operation(
                old.operations[key], new.operations[key], rename_threshold=rename_threshold
            )
        )
    return changes


def _diff_operation(old: Operation, new: Operation, *, rename_threshold: float) -> list[ApiChange]:
    changes: list[ApiChange] = []
    if not old.deprecated and new.deprecated:
        changes.append(
            _operation_change(new, ChangeType.OPERATION_DEPRECATED, Severity.POTENTIALLY_BREAKING)
        )
    changes.extend(_diff_parameters(old, new, rename_threshold=rename_threshold))
    changes.extend(_diff_request_body(old, new, rename_threshold=rename_threshold))
    changes.extend(_diff_responses(old, new, rename_threshold=rename_threshold))
    return changes


# -------------------------------------------------------------- parameters ---


def _diff_parameters(old: Operation, new: Operation, *, rename_threshold: float) -> list[ApiChange]:
    changes: list[ApiChange] = []
    removed_keys = sorted(set(old.parameters) - set(new.parameters))
    added_keys = sorted(set(new.parameters) - set(old.parameters))

    # Renames are only sought within one location: a query parameter becoming a
    # header is a transport change, not a rename, and must not be collapsed.
    replacements: dict[str, str] = {}
    rename_targets: set[str] = set()
    for location in ParameterLocation:
        removed = {
            name: old.parameters[(name, loc)].json_schema
            for name, loc in removed_keys
            if loc is location
        }
        added = {
            name: new.parameters[(name, loc)].json_schema
            for name, loc in added_keys
            if loc is location
        }
        for candidate in detect_renames(removed, added, threshold=rename_threshold):
            replacements[f"{candidate.old_name}\x00{location.value}"] = candidate.new_name
            rename_targets.add(f"{candidate.new_name}\x00{location.value}")

    for name, location in removed_keys:
        parameter = old.parameters[(name, location)]
        replacement = replacements.get(f"{name}\x00{location.value}")
        detail = f"{old.endpoint}: {location.value} parameter {name!r} removed"
        if replacement:
            detail += f"; replaced by {replacement!r}"
        changes.append(
            _parameter_change(
                old,
                parameter,
                ChangeType.PARAMETER_REMOVED,
                Severity.BREAKING,
                detail,
                replacement=replacement,
            )
        )

    for name, location in added_keys:
        parameter = new.parameters[(name, location)]
        is_rename_target = f"{name}\x00{location.value}" in rename_targets
        if parameter.required:
            change_type = ChangeType.REQUIRED_PARAMETER_ADDED
            severity = Severity.BREAKING
            detail = f"{new.endpoint}: required {location.value} parameter {name!r} added"
        else:
            change_type = ChangeType.PARAMETER_ADDED
            # An unrelated optional parameter breaks nothing. The same addition
            # as the target of a rename is part of a migration the caller must
            # perform, so it is worth surfacing.
            severity = Severity.POTENTIALLY_BREAKING if is_rename_target else Severity.NON_BREAKING
            detail = f"{new.endpoint}: optional {location.value} parameter {name!r} added"
            if is_rename_target:
                detail += " (replaces a removed parameter)"
        changes.append(_parameter_change(new, parameter, change_type, severity, detail))

    for key in sorted(set(old.parameters) & set(new.parameters)):
        changes.extend(_diff_parameter(old, new, old.parameters[key], new.parameters[key]))

    return changes


def _diff_parameter(
    old_operation: Operation, new_operation: Operation, old: Parameter, new: Parameter
) -> list[ApiChange]:
    changes: list[ApiChange] = []
    endpoint = new_operation.endpoint

    if not old.required and new.required:
        changes.append(
            _parameter_change(
                new_operation,
                new,
                ChangeType.PARAMETER_BECAME_REQUIRED,
                Severity.BREAKING,
                f"{endpoint}: parameter {new.name!r} is now required",
            )
        )
    elif old.required and not new.required:
        changes.append(
            _parameter_change(
                new_operation,
                new,
                ChangeType.PARAMETER_BECAME_OPTIONAL,
                Severity.NON_BREAKING,
                f"{endpoint}: parameter {new.name!r} is no longer required",
            )
        )

    if not old.deprecated and new.deprecated:
        changes.append(
            _parameter_change(
                new_operation,
                new,
                ChangeType.PARAMETER_DEPRECATED,
                Severity.POTENTIALLY_BREAKING,
                f"{endpoint}: parameter {new.name!r} is deprecated",
            )
        )

    for finding in diff_schema(old.json_schema, new.json_schema):
        change_type = (
            ChangeType.PARAMETER_TYPE_CHANGED
            if finding.is_root and finding.kind is SchemaChangeKind.TYPE_CHANGED
            else ChangeType.PARAMETER_SCHEMA_CHANGED
        )
        field_path = f"{new.name}.{finding.path}" if finding.path else new.name
        changes.append(
            ApiChange(
                type=change_type,
                severity=severity_for(SchemaDirection.REQUEST, finding.kind),
                endpoint=endpoint,
                path=new_operation.path,
                method=new_operation.method.value.upper(),
                location=_LOCATION_BY_PARAMETER[new.location],
                field=new.name if finding.is_root else finding.leaf,
                field_path=field_path,
                old_value=finding.old,
                new_value=finding.new,
                detail=(
                    f"{endpoint}: parameter {new.name!r} schema "
                    f"{finding.kind.value.replace('_', ' ')} at {field_path!r}"
                ),
            )
        )
    return changes


def _parameter_change(
    operation: Operation,
    parameter: Parameter,
    change_type: ChangeType,
    severity: Severity,
    detail: str,
    *,
    replacement: str | None = None,
) -> ApiChange:
    return ApiChange(
        type=change_type,
        severity=severity,
        endpoint=operation.endpoint,
        path=operation.path,
        method=operation.method.value.upper(),
        location=_LOCATION_BY_PARAMETER[parameter.location],
        field=parameter.name,
        field_path=parameter.name,
        replacement=replacement,
        detail=detail,
    )


# ------------------------------------------------------------ request body ---


def _diff_request_body(
    old: Operation, new: Operation, *, rename_threshold: float
) -> list[ApiChange]:
    if old.request_body is None and new.request_body is None:
        return []

    if old.request_body is None and new.request_body is not None:
        required = new.request_body.required
        return [
            _body_change(
                new,
                ChangeType.REQUEST_BODY_ADDED,
                Severity.BREAKING if required else Severity.POTENTIALLY_BREAKING,
                f"{new.endpoint}: {'required' if required else 'optional'} request body added",
            )
        ]

    if old.request_body is not None and new.request_body is None:
        return [
            _body_change(
                new,
                ChangeType.REQUEST_BODY_REMOVED,
                Severity.BREAKING,
                f"{new.endpoint}: request body removed",
            )
        ]

    assert old.request_body is not None and new.request_body is not None  # noqa: S101
    changes: list[ApiChange] = []
    if not old.request_body.required and new.request_body.required:
        changes.append(
            _body_change(
                new,
                ChangeType.REQUEST_BODY_BECAME_REQUIRED,
                Severity.BREAKING,
                f"{new.endpoint}: request body is now required",
            )
        )
    elif old.request_body.required and not new.request_body.required:
        changes.append(
            _body_change(
                new,
                ChangeType.REQUEST_BODY_BECAME_OPTIONAL,
                Severity.NON_BREAKING,
                f"{new.endpoint}: request body is no longer required",
            )
        )

    changes.extend(
        _diff_content(
            new,
            old.request_body,
            new.request_body,
            direction=SchemaDirection.REQUEST,
            rename_threshold=rename_threshold,
        )
    )
    return changes


def _body_change(
    operation: Operation, change_type: ChangeType, severity: Severity, detail: str
) -> ApiChange:
    return ApiChange(
        type=change_type,
        severity=severity,
        endpoint=operation.endpoint,
        path=operation.path,
        method=operation.method.value.upper(),
        location=ChangeLocation.REQUEST_BODY,
        detail=detail,
    )


# --------------------------------------------------------------- responses ---


def _diff_responses(old: Operation, new: Operation, *, rename_threshold: float) -> list[ApiChange]:
    changes: list[ApiChange] = []

    for status_code in sorted(set(old.responses) - set(new.responses)):
        # Losing a documented success response breaks any client that reads it.
        # Losing a documented error response usually only changes what the
        # client should expect on failure, so it is graded a step lower.
        severity = (
            Severity.BREAKING if status_code.startswith("2") else Severity.POTENTIALLY_BREAKING
        )
        changes.append(
            _response_change(
                new,
                ChangeType.RESPONSE_REMOVED,
                severity,
                status_code,
                f"{new.endpoint}: response {status_code} removed",
            )
        )

    for status_code in sorted(set(new.responses) - set(old.responses)):
        changes.append(
            _response_change(
                new,
                ChangeType.RESPONSE_ADDED,
                Severity.NON_BREAKING,
                status_code,
                f"{new.endpoint}: response {status_code} added",
            )
        )

    for status_code in sorted(set(old.responses) & set(new.responses)):
        changes.extend(
            _diff_content(
                new,
                old.responses[status_code],
                new.responses[status_code],
                direction=SchemaDirection.RESPONSE,
                rename_threshold=rename_threshold,
                status_code=status_code,
            )
        )
    return changes


def _response_change(
    operation: Operation,
    change_type: ChangeType,
    severity: Severity,
    status_code: str,
    detail: str,
) -> ApiChange:
    return ApiChange(
        type=change_type,
        severity=severity,
        endpoint=operation.endpoint,
        path=operation.path,
        method=operation.method.value.upper(),
        location=ChangeLocation.RESPONSE,
        status_code=status_code,
        detail=detail,
    )


# --------------------------------------------------------- content/schemas ---


def _diff_content(
    operation: Operation,
    old: Body,
    new: Body,
    *,
    direction: SchemaDirection,
    rename_threshold: float,
    status_code: str | None = None,
) -> list[ApiChange]:
    """Compare the media types of one body and the schemas underneath them."""
    is_request = direction is SchemaDirection.REQUEST
    added_type = (
        ChangeType.REQUEST_CONTENT_TYPE_ADDED
        if is_request
        else ChangeType.RESPONSE_CONTENT_TYPE_ADDED
    )
    removed_type = (
        ChangeType.REQUEST_CONTENT_TYPE_REMOVED
        if is_request
        else ChangeType.RESPONSE_CONTENT_TYPE_REMOVED
    )
    location = ChangeLocation.REQUEST_BODY if is_request else ChangeLocation.RESPONSE

    changes: list[ApiChange] = []
    for content_type in sorted(set(old.content) - set(new.content)):
        changes.append(
            _content_change(
                operation,
                removed_type,
                Severity.BREAKING,
                location,
                content_type,
                status_code,
                f"{operation.endpoint}: media type {content_type!r} no longer supported",
            )
        )
    for content_type in sorted(set(new.content) - set(old.content)):
        changes.append(
            _content_change(
                operation,
                added_type,
                Severity.NON_BREAKING,
                location,
                content_type,
                status_code,
                f"{operation.endpoint}: media type {content_type!r} added",
            )
        )

    for content_type in sorted(set(old.content) & set(new.content)):
        changes.extend(
            _diff_body_schema(
                operation,
                old.content[content_type],
                new.content[content_type],
                direction=direction,
                content_type=content_type,
                status_code=status_code,
                rename_threshold=rename_threshold,
            )
        )
    return changes


def _content_change(
    operation: Operation,
    change_type: ChangeType,
    severity: Severity,
    location: ChangeLocation,
    content_type: str,
    status_code: str | None,
    detail: str,
) -> ApiChange:
    return ApiChange(
        type=change_type,
        severity=severity,
        endpoint=operation.endpoint,
        path=operation.path,
        method=operation.method.value.upper(),
        location=location,
        content_type=content_type,
        status_code=status_code,
        detail=detail,
    )


def _top_level_properties(schema: JsonSchema) -> dict[str, JsonSchema]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {str(name): value for name, value in properties.items() if isinstance(value, dict)}


def _diff_body_schema(
    operation: Operation,
    old_schema: JsonSchema,
    new_schema: JsonSchema,
    *,
    direction: SchemaDirection,
    content_type: str,
    status_code: str | None,
    rename_threshold: float,
) -> list[ApiChange]:
    findings = diff_schema(old_schema, new_schema)
    if not findings:
        return []

    # Rename linking is applied to top-level properties only. Nested renames are
    # ambiguous -- the same leaf name can occur under several parents -- and a
    # wrong link is worse than none.
    old_properties = _top_level_properties(old_schema)
    new_properties = _top_level_properties(new_schema)
    removed = {
        name: old_properties[name]
        for finding in findings
        if finding.kind is SchemaChangeKind.FIELD_REMOVED
        and (name := finding.path) in old_properties
    }
    added = {
        name: new_properties[name]
        for finding in findings
        if finding.kind is SchemaChangeKind.FIELD_ADDED and (name := finding.path) in new_properties
    }
    replacements = {
        candidate.old_name: candidate.new_name
        for candidate in detect_renames(removed, added, threshold=rename_threshold)
    }
    rename_targets = set(replacements.values())

    is_request = direction is SchemaDirection.REQUEST
    type_map = _REQUEST_CHANGE_TYPES if is_request else _RESPONSE_CHANGE_TYPES
    schema_level = (
        ChangeType.REQUEST_SCHEMA_CHANGED if is_request else ChangeType.RESPONSE_SCHEMA_CHANGED
    )
    location = ChangeLocation.REQUEST_BODY if is_request else ChangeLocation.RESPONSE

    return [
        _schema_finding_to_change(
            operation,
            finding,
            direction=direction,
            change_type=(
                schema_level
                if finding.is_root or finding.kind not in type_map
                else type_map[finding.kind]
            ),
            location=location,
            content_type=content_type,
            status_code=status_code,
            replacement=replacements.get(finding.path),
            is_rename_target=finding.path in rename_targets
            and finding.kind is SchemaChangeKind.FIELD_ADDED,
        )
        for finding in findings
    ]


def _schema_finding_to_change(
    operation: Operation,
    finding: SchemaChange,
    *,
    direction: SchemaDirection,
    change_type: ChangeType,
    location: ChangeLocation,
    content_type: str,
    status_code: str | None,
    replacement: str | None,
    is_rename_target: bool,
) -> ApiChange:
    severity = severity_for(direction, finding.kind)
    if is_rename_target and severity is Severity.NON_BREAKING:
        severity = Severity.POTENTIALLY_BREAKING

    target = finding.path or "the schema root"
    detail = (
        f"{operation.endpoint}: {location.value} {finding.kind.value.replace('_', ' ')} at {target}"
    )
    if status_code:
        detail = f"{detail} (response {status_code})"
    if replacement:
        detail = f"{detail}; replaced by {replacement!r}"
    elif is_rename_target:
        detail = f"{detail} (replaces a removed field)"

    return ApiChange(
        type=change_type,
        severity=severity,
        endpoint=operation.endpoint,
        path=operation.path,
        method=operation.method.value.upper(),
        location=location,
        field_path=finding.path or None,
        replacement=replacement,
        content_type=content_type,
        status_code=status_code,
        old_value=finding.old,
        new_value=finding.new,
        detail=detail,
    )


__all__ = ["diff_specs"]
