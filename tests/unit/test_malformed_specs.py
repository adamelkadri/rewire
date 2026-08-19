"""Specifications are untrusted input; malformed ones must degrade, not crash.

Every case here is a document that is syntactically valid YAML but structurally
wrong in a way a real-world specification can be. Rewire must skip what it
cannot interpret and still return a usable spec, because refusing to load an
entire document over one malformed operation would make it useless against the
long tail of published APIs.
"""

from __future__ import annotations

from rewire.changes.spec import ParameterLocation, parse_spec_text

HEAD = 'openapi: "3.0.3"\ninfo: {title: T, version: "1"}\n'


def test_non_mapping_path_item_is_skipped() -> None:
    spec = parse_spec_text(
        HEAD + 'paths:\n  /a: "not an object"\n  /b:\n    get:\n      responses: {}\n'
    )
    assert set(spec.operations) == {"GET /b"}


def test_non_mapping_operation_is_skipped() -> None:
    spec = parse_spec_text(HEAD + "paths:\n  /a:\n    get: null\n    post:\n      responses: {}\n")
    assert set(spec.operations) == {"POST /a"}


def test_non_mapping_parameter_entry_is_skipped() -> None:
    spec = parse_spec_text(
        HEAD + "paths:\n  /a:\n    get:\n      parameters:\n"
        "        - just-a-string\n"
        "        - {name: ok, in: query, schema: {type: string}}\n"
        "      responses: {}\n"
    )
    assert {name for name, _ in spec.operations["GET /a"].parameters} == {"ok"}


def test_parameter_without_name_or_location_is_skipped() -> None:
    spec = parse_spec_text(
        HEAD + "paths:\n  /a:\n    get:\n      parameters:\n"
        "        - {in: query, schema: {type: string}}\n"
        "        - {name: nameless}\n"
        "        - {name: 42, in: query}\n"
        "      responses: {}\n"
    )
    assert spec.operations["GET /a"].parameters == {}


def test_non_mapping_request_body_yields_no_body() -> None:
    spec = parse_spec_text(
        HEAD + 'paths:\n  /a:\n    post:\n      requestBody: "nonsense"\n      responses: {}\n'
    )
    assert spec.operations["POST /a"].request_body is None


def test_non_mapping_content_is_ignored() -> None:
    spec = parse_spec_text(
        HEAD
        + "paths:\n  /a:\n    post:\n      requestBody:\n        content: []\n      responses: {}\n"
    )
    body = spec.operations["POST /a"].request_body
    assert body is not None and body.content == {}


def test_non_string_media_type_is_skipped() -> None:
    spec = parse_spec_text(
        HEAD + "paths:\n  /a:\n    post:\n      requestBody:\n        content:\n"
        "          200: {schema: {type: object}}\n"
        "          application/json: {schema: {type: object}}\n"
        "      responses: {}\n"
    )
    body = spec.operations["POST /a"].request_body
    assert body is not None and set(body.content) == {"application/json"}


def test_media_type_without_a_schema_yields_an_empty_schema() -> None:
    spec = parse_spec_text(
        HEAD + "paths:\n  /a:\n    post:\n      requestBody:\n        content:\n"
        "          application/json: {example: {a: 1}}\n      responses: {}\n"
    )
    body = spec.operations["POST /a"].request_body
    assert body is not None and body.content["application/json"] == {}


def test_non_mapping_responses_are_ignored() -> None:
    spec = parse_spec_text(HEAD + "paths:\n  /a:\n    get:\n      responses: []\n")
    assert spec.operations["GET /a"].responses == {}


def test_non_mapping_response_entry_is_skipped() -> None:
    spec = parse_spec_text(
        HEAD + "paths:\n  /a:\n    get:\n      responses:\n"
        "        '200': null\n        '404': {description: Missing}\n"
    )
    assert set(spec.operations["GET /a"].responses) == {"404"}


def test_numeric_status_codes_are_normalised_to_strings() -> None:
    spec = parse_spec_text(
        HEAD + "paths:\n  /a:\n    get:\n      responses:\n        200: {description: OK}\n"
    )
    assert set(spec.operations["GET /a"].responses) == {"200"}


def test_non_string_operation_id_and_summary_are_dropped() -> None:
    document = HEAD + (
        "paths:\n  /a:\n    get:\n      operationId: 42\n"
        "      summary: [a, b]\n      responses: {}\n"
    )
    spec = parse_spec_text(document)
    operation = spec.operations["GET /a"]
    assert operation.operation_id is None
    assert operation.summary is None


def test_parameter_without_a_schema_gets_an_empty_one() -> None:
    spec = parse_spec_text(
        HEAD + "paths:\n  /a:\n    get:\n      parameters:\n"
        "        - {name: p, in: query}\n      responses: {}\n"
    )
    assert spec.operations["GET /a"].parameters[("p", ParameterLocation.QUERY)].json_schema == {}


def test_missing_info_block_is_tolerated() -> None:
    spec = parse_spec_text('openapi: "3.0.3"\npaths:\n  /a:\n    get:\n      responses: {}\n')
    assert spec.metadata.title is None
    assert spec.metadata.version is None
    assert set(spec.operations) == {"GET /a"}


def test_non_mapping_info_block_is_tolerated() -> None:
    spec = parse_spec_text('openapi: "3.0.3"\ninfo: "just a title"\npaths: {}\n')
    assert spec.metadata.title is None


def test_numeric_info_version_is_stringified() -> None:
    spec = parse_spec_text('openapi: "3.0.3"\ninfo: {title: T, version: 2}\npaths: {}\n')
    assert spec.metadata.version == "2"
