"""Tests for the specification differ."""

from __future__ import annotations

import pytest

from rewire.changes import ChangeType, Severity, diff_specs, parse_spec_text
from rewire.changes.models import ChangeLocation

HEAD = 'openapi: "3.0.3"\ninfo: {title: T, version: "1"}\n'


def build(paths: str) -> object:
    return parse_spec_text(HEAD + "paths:\n" + paths)


def diff(old_paths: str, new_paths: str) -> list:
    return diff_specs(build(old_paths), build(new_paths)).changes  # type: ignore[arg-type]


def types(old_paths: str, new_paths: str) -> list[ChangeType]:
    return [change.type for change in diff(old_paths, new_paths)]


SIMPLE_GET = "  /a:\n    get:\n      responses: {'200': {description: OK}}\n"


# ------------------------------------------------------------- operations ---


def test_identical_specs_produce_no_changes() -> None:
    report = diff_specs(build(SIMPLE_GET), build(SIMPLE_GET))
    assert report.changes == []
    assert report.summary.total == 0
    assert report.has_breaking_changes is False


def test_endpoint_removed_is_breaking() -> None:
    changes = diff(SIMPLE_GET, "  /b:\n    get:\n      responses: {}\n")
    by_type = {change.type: change for change in changes}
    assert by_type[ChangeType.ENDPOINT_REMOVED].severity is Severity.BREAKING
    assert by_type[ChangeType.ENDPOINT_ADDED].severity is Severity.NON_BREAKING


def test_losing_one_method_is_an_operation_change_not_an_endpoint_change() -> None:
    """The URL still exists, so reporting the endpoint as removed would mislead."""
    old = "  /a:\n    get:\n      responses: {}\n    post:\n      responses: {}\n"
    new = "  /a:\n    get:\n      responses: {}\n"
    assert types(old, new) == [ChangeType.OPERATION_REMOVED]


def test_losing_the_last_method_is_an_endpoint_change() -> None:
    assert ChangeType.ENDPOINT_REMOVED in types(
        SIMPLE_GET, "  /b:\n    get:\n      responses: {}\n"
    )


def test_method_added_to_existing_path() -> None:
    new = SIMPLE_GET + "    post:\n      responses: {}\n"
    assert types(SIMPLE_GET, new) == [ChangeType.OPERATION_ADDED]


def test_operation_deprecation_is_potentially_breaking() -> None:
    new = SIMPLE_GET.replace("    get:\n", "    get:\n      deprecated: true\n")
    changes = diff(SIMPLE_GET, new)
    assert changes[0].type is ChangeType.OPERATION_DEPRECATED
    assert changes[0].severity is Severity.POTENTIALLY_BREAKING


def test_change_carries_endpoint_path_and_method() -> None:
    change = diff(SIMPLE_GET, "  /b:\n    get:\n      responses: {}\n")[0]
    assert change.endpoint == "GET /a"
    assert change.path == "/a"
    assert change.method == "GET"
    assert change.location is ChangeLocation.OPERATION


# ------------------------------------------------------------- parameters ---


def param(name: str, *, required: bool = False, schema: str = "{type: string}") -> str:
    flag = str(required).lower()
    return (
        "  /a:\n    get:\n      parameters:\n"
        f"        - {{name: {name}, in: query, required: {flag}, schema: {schema}}}\n"
        "      responses: {}\n"
    )


def test_parameter_removed_is_breaking() -> None:
    changes = diff(param("p"), "  /a:\n    get:\n      responses: {}\n")
    assert changes[0].type is ChangeType.PARAMETER_REMOVED
    assert changes[0].severity is Severity.BREAKING
    assert changes[0].field == "p"


def test_optional_parameter_added_is_non_breaking() -> None:
    changes = diff("  /a:\n    get:\n      responses: {}\n", param("p"))
    assert changes[0].type is ChangeType.PARAMETER_ADDED
    assert changes[0].severity is Severity.NON_BREAKING


def test_required_parameter_added_is_breaking() -> None:
    changes = diff("  /a:\n    get:\n      responses: {}\n", param("p", required=True))
    assert changes[0].type is ChangeType.REQUIRED_PARAMETER_ADDED
    assert changes[0].severity is Severity.BREAKING


def test_parameter_became_required() -> None:
    changes = diff(param("p"), param("p", required=True))
    assert changes[0].type is ChangeType.PARAMETER_BECAME_REQUIRED
    assert changes[0].severity is Severity.BREAKING


def test_parameter_became_optional_is_non_breaking() -> None:
    changes = diff(param("p", required=True), param("p"))
    assert changes[0].type is ChangeType.PARAMETER_BECAME_OPTIONAL
    assert changes[0].severity is Severity.NON_BREAKING


def test_parameter_type_change_is_breaking() -> None:
    changes = diff(param("p"), param("p", schema="{type: integer}"))
    assert changes[0].type is ChangeType.PARAMETER_TYPE_CHANGED
    assert changes[0].severity is Severity.BREAKING


def test_parameter_constraint_change_is_potentially_breaking() -> None:
    changes = diff(
        param("p", schema="{type: integer, maximum: 100}"),
        param("p", schema="{type: integer, maximum: 50}"),
    )
    assert changes[0].type is ChangeType.PARAMETER_SCHEMA_CHANGED
    assert changes[0].severity is Severity.POTENTIALLY_BREAKING


def test_parameter_deprecation() -> None:
    old = param("p")
    new = old.replace("required: false", "required: false, deprecated: true")
    changes = diff(old, new)
    assert changes[0].type is ChangeType.PARAMETER_DEPRECATED
    assert changes[0].severity is Severity.POTENTIALLY_BREAKING


def test_moving_a_parameter_between_locations_is_not_a_rename() -> None:
    """A query parameter becoming a header changes the wire format, not the name."""
    old = param("token")
    new = (
        "  /a:\n    get:\n      parameters:\n"
        "        - {name: token, in: header, schema: {type: string}}\n"
        "      responses: {}\n"
    )
    changes = diff(old, new)
    removed = next(c for c in changes if c.type is ChangeType.PARAMETER_REMOVED)
    assert removed.replacement is None


# ----------------------------------------------------------------- renames ---


def test_parameter_rename_links_removal_to_addition() -> None:
    changes = diff(
        param("max_tokens", schema="{type: integer}"),
        param("max_output_tokens", schema="{type: integer}"),
    )
    removed = next(c for c in changes if c.type is ChangeType.PARAMETER_REMOVED)
    added = next(c for c in changes if c.type is ChangeType.PARAMETER_ADDED)

    assert removed.field == "max_tokens"
    assert removed.replacement == "max_output_tokens"
    assert removed.severity is Severity.BREAKING
    # The addition is part of a migration, not an unrelated new option.
    assert added.severity is Severity.POTENTIALLY_BREAKING


def test_rename_threshold_can_disable_linking() -> None:
    old, new = (
        param("max_tokens", schema="{type: integer}"),
        param("max_output_tokens", schema="{type: integer}"),
    )
    report = diff_specs(build(old), build(new), rename_threshold=1.1)
    removed = next(c for c in report.changes if c.type is ChangeType.PARAMETER_REMOVED)
    added = next(c for c in report.changes if c.type is ChangeType.PARAMETER_ADDED)
    assert removed.replacement is None
    assert added.severity is Severity.NON_BREAKING


# -------------------------------------------------------------- body/schema --


def body(properties: str, *, required: str = "[]", body_required: bool = True) -> str:
    return (
        "  /a:\n    post:\n      requestBody:\n"
        f"        required: {str(body_required).lower()}\n"
        "        content:\n          application/json:\n"
        "            schema:\n              type: object\n"
        f"              required: {required}\n"
        f"              properties:\n{properties}"
        "      responses: {}\n"
    )


def test_request_body_added_and_removed() -> None:
    empty = "  /a:\n    post:\n      responses: {}\n"
    added = diff(empty, body("                a: {type: string}\n"))
    assert added[0].type is ChangeType.REQUEST_BODY_ADDED
    assert added[0].severity is Severity.BREAKING

    removed = diff(body("                a: {type: string}\n"), empty)
    assert removed[0].type is ChangeType.REQUEST_BODY_REMOVED
    assert removed[0].severity is Severity.BREAKING


def test_optional_request_body_added_is_potentially_breaking() -> None:
    empty = "  /a:\n    post:\n      responses: {}\n"
    changes = diff(empty, body("                a: {type: string}\n", body_required=False))
    assert changes[0].severity is Severity.POTENTIALLY_BREAKING


def test_request_body_requirement_transitions() -> None:
    optional = body("                a: {type: string}\n", body_required=False)
    required = body("                a: {type: string}\n", body_required=True)
    assert types(optional, required) == [ChangeType.REQUEST_BODY_BECAME_REQUIRED]
    assert types(required, optional) == [ChangeType.REQUEST_BODY_BECAME_OPTIONAL]


def test_request_field_changes_map_to_request_types() -> None:
    old = body("                a: {type: string}\n")
    new = body("                a: {type: string}\n                b: {type: string}\n")
    assert types(old, new) == [ChangeType.REQUEST_FIELD_ADDED]
    assert types(new, old) == [ChangeType.REQUEST_FIELD_REMOVED]


def test_request_field_became_required_is_breaking() -> None:
    old = body("                a: {type: string}\n")
    new = body("                a: {type: string}\n", required="[a]")
    changes = diff(old, new)
    assert changes[0].type is ChangeType.REQUEST_FIELD_BECAME_REQUIRED
    assert changes[0].severity is Severity.BREAKING


def test_request_body_field_rename_is_linked() -> None:
    old = body("                max_tokens: {type: integer}\n")
    new = body("                max_completion_tokens: {type: integer}\n")
    changes = diff(old, new)
    removed = next(c for c in changes if c.type is ChangeType.REQUEST_FIELD_REMOVED)
    added = next(c for c in changes if c.type is ChangeType.REQUEST_FIELD_ADDED)
    assert removed.replacement == "max_completion_tokens"
    assert added.severity is Severity.POTENTIALLY_BREAKING


def test_root_level_schema_change_uses_the_schema_level_type() -> None:
    old = body("                a: {type: string}\n")
    new = old.replace("type: object", "type: array")
    assert ChangeType.REQUEST_SCHEMA_CHANGED in types(old, new)


def test_request_media_type_removal_is_breaking() -> None:
    old = (
        "  /a:\n    post:\n      requestBody:\n        content:\n"
        "          application/json: {schema: {type: object}}\n"
        "          application/xml: {schema: {type: object}}\n      responses: {}\n"
    )
    new = (
        "  /a:\n    post:\n      requestBody:\n        content:\n"
        "          application/json: {schema: {type: object}}\n      responses: {}\n"
    )
    changes = diff(old, new)
    assert changes[0].type is ChangeType.REQUEST_CONTENT_TYPE_REMOVED
    assert changes[0].content_type == "application/xml"
    assert changes[0].severity is Severity.BREAKING


# ------------------------------------------------------------------ responses -


def response(status: str, properties: str, *, required: str = "[]") -> str:
    return (
        f"  /a:\n    get:\n      responses:\n        '{status}':\n"
        "          description: OK\n          content:\n            application/json:\n"
        "              schema:\n                type: object\n"
        f"                required: {required}\n                properties:\n{properties}"
    )


def test_success_response_removal_is_breaking() -> None:
    old = response("200", "                  a: {type: string}\n")
    new = "  /a:\n    get:\n      responses: {'404': {description: Missing}}\n"
    removed = next(c for c in diff(old, new) if c.type is ChangeType.RESPONSE_REMOVED)
    assert removed.severity is Severity.BREAKING
    assert removed.status_code == "200"


def test_error_response_removal_is_graded_lower() -> None:
    old = (
        "  /a:\n    get:\n      responses:\n        '200': {description: OK}\n"
        "        '404': {description: Missing}\n"
    )
    new = "  /a:\n    get:\n      responses:\n        '200': {description: OK}\n"
    removed = next(c for c in diff(old, new) if c.type is ChangeType.RESPONSE_REMOVED)
    assert removed.severity is Severity.POTENTIALLY_BREAKING


def test_response_added_is_non_breaking() -> None:
    old = "  /a:\n    get:\n      responses:\n        '200': {description: OK}\n"
    new = old + "        '429': {description: Slow down}\n"
    changes = diff(old, new)
    assert changes[0].type is ChangeType.RESPONSE_ADDED
    assert changes[0].severity is Severity.NON_BREAKING


def test_response_field_removed_is_breaking() -> None:
    old = response(
        "200", "                  a: {type: string}\n                  b: {type: string}\n"
    )
    new = response("200", "                  a: {type: string}\n")
    changes = diff(old, new)
    assert changes[0].type is ChangeType.RESPONSE_FIELD_REMOVED
    assert changes[0].severity is Severity.BREAKING
    assert changes[0].status_code == "200"


def test_response_field_became_optional_is_breaking() -> None:
    """The field may now be absent, so every unguarded read of it is a latent bug."""
    old = response("200", "                  a: {type: string}\n", required="[a]")
    new = response("200", "                  a: {type: string}\n")
    changes = diff(old, new)
    assert changes[0].type is ChangeType.RESPONSE_FIELD_BECAME_OPTIONAL
    assert changes[0].severity is Severity.BREAKING


def test_response_field_became_required_is_non_breaking() -> None:
    old = response("200", "                  a: {type: string}\n")
    new = response("200", "                  a: {type: string}\n", required="[a]")
    changes = diff(old, new)
    assert changes[0].type is ChangeType.RESPONSE_FIELD_BECAME_REQUIRED
    assert changes[0].severity is Severity.NON_BREAKING


def test_the_same_edit_is_graded_differently_by_direction() -> None:
    """The central claim of the severity model, asserted directly."""
    request_change = diff(
        body("                a: {type: string}\n", required="[a]"),
        body("                a: {type: string}\n"),
    )
    response_change = diff(
        response("200", "                  a: {type: string}\n", required="[a]"),
        response("200", "                  a: {type: string}\n"),
    )
    assert request_change[0].severity is Severity.NON_BREAKING
    assert response_change[0].severity is Severity.BREAKING


# ------------------------------------------------------------------- report --


def test_report_records_both_spec_versions() -> None:
    old = parse_spec_text(HEAD + "paths:\n" + SIMPLE_GET, source="old.yaml")
    new = parse_spec_text(
        'openapi: "3.0.3"\ninfo: {title: T, version: "2"}\npaths:\n' + SIMPLE_GET, source="new.yaml"
    )
    report = diff_specs(old, new)
    assert report.old_spec.version == "1"
    assert report.new_spec.version == "2"
    assert report.new_spec.source == "new.yaml"


def test_changes_are_sorted_most_severe_first() -> None:
    old = "  /a:\n    get:\n      responses: {}\n  /b:\n    get:\n      responses: {}\n"
    new = "  /a:\n    get:\n      responses: {}\n  /c:\n    get:\n      responses: {}\n"
    report = diff_specs(build(old), build(new))
    ranks = [change.severity.rank for change in report.changes]
    assert ranks == sorted(ranks)


def test_diff_is_deterministic() -> None:
    old = "  /a:\n    get:\n      responses: {}\n  /b:\n    post:\n      responses: {}\n"
    new = "  /a:\n    put:\n      responses: {}\n  /c:\n    get:\n      responses: {}\n"
    first = diff_specs(build(old), build(new)).model_dump_json()
    second = diff_specs(build(old), build(new)).model_dump_json()
    assert first == second


@pytest.mark.parametrize("severity", list(Severity))
def test_filter_returns_only_changes_at_or_above_severity(severity: Severity) -> None:
    report = diff_specs(
        build("  /a:\n    get:\n      responses: {}\n"),
        build("  /b:\n    get:\n      responses: {}\n"),
    )
    assert all(change.severity.rank <= severity.rank for change in report.filter(severity))


def test_for_endpoint_selects_a_single_endpoint() -> None:
    old = "  /a:\n    get:\n      responses: {}\n  /b:\n    get:\n      responses: {}\n"
    new = "  /c:\n    get:\n      responses: {}\n"
    report = diff_specs(build(old), build(new))
    assert {c.endpoint for c in report.for_endpoint("GET /a")} == {"GET /a"}


def test_response_media_type_added_is_non_breaking() -> None:
    old = (
        "  /a:\n    get:\n      responses:\n        '200':\n          description: OK\n"
        "          content:\n            application/json: {schema: {type: object}}\n"
    )
    new = old + "            text/csv: {schema: {type: string}}\n"
    changes = diff(old, new)
    assert changes[0].type is ChangeType.RESPONSE_CONTENT_TYPE_ADDED
    assert changes[0].content_type == "text/csv"
    assert changes[0].severity is Severity.NON_BREAKING
