"""Tests for OpenAPI loading, normalisation and $ref resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from rewire.changes.spec import (
    CIRCULAR_REF_KEY,
    MAX_REF_DEPTH,
    HttpMethod,
    ParameterLocation,
    load_spec,
    parse_spec_text,
)
from rewire.core.errors import SpecParseError

MINIMAL = """
openapi: 3.0.3
info: {title: Demo, version: "1.0"}
paths:
  /items:
    get:
      responses: {'200': {description: OK}}
"""


def test_parses_yaml() -> None:
    spec = parse_spec_text(MINIMAL)
    assert spec.metadata.title == "Demo"
    assert spec.metadata.version == "1.0"
    assert spec.metadata.openapi_version == "3.0.3"
    assert set(spec.operations) == {"GET /items"}


def test_parses_json() -> None:
    spec = parse_spec_text(
        '{"openapi":"3.0.3","info":{"title":"D","version":"1"},'
        '"paths":{"/x":{"get":{"responses":{}}}}}'
    )
    assert set(spec.operations) == {"GET /x"}


def test_yaml_and_json_normalise_identically() -> None:
    """The same API in two syntaxes must produce the same normalised spec."""
    from_yaml = parse_spec_text(MINIMAL)
    from_json = parse_spec_text(
        '{"openapi":"3.0.3","info":{"title":"Demo","version":"1.0"},'
        '"paths":{"/items":{"get":{"responses":{"200":{"description":"OK"}}}}}}'
    )
    assert from_yaml.operations == from_json.operations


def test_loads_from_disk(specs: Path) -> None:
    spec = load_spec(specs / "openai" / "chat_old.yaml")
    assert "POST /v1/chat/completions" in spec.operations


def test_missing_file_raises() -> None:
    with pytest.raises(SpecParseError, match="could not read"):
        load_spec("does/not/exist.yaml")


def test_oversized_file_is_refused(tmp_path: Path) -> None:
    spec_file = tmp_path / "big.yaml"
    spec_file.write_text(MINIMAL, encoding="utf-8")
    with pytest.raises(SpecParseError, match="larger than the safety limit"):
        load_spec(spec_file, max_bytes=10)


def test_non_utf8_file_raises(tmp_path: Path) -> None:
    spec_file = tmp_path / "bad.yaml"
    spec_file.write_bytes(b"\xff\xfe\x00invalid")
    with pytest.raises(SpecParseError, match="could not read"):
        load_spec(spec_file)


# ------------------------------------------------------------- rejections ---


def test_swagger_2_is_rejected_with_guidance(specs: Path) -> None:
    with pytest.raises(SpecParseError, match=r"Swagger 2\.0"):
        load_spec(specs / "invalid" / "swagger2.yaml")


def test_non_openapi_document_is_rejected(specs: Path) -> None:
    with pytest.raises(SpecParseError, match="not an OpenAPI specification"):
        load_spec(specs / "invalid" / "not_openapi.yaml")


def test_malformed_yaml_is_rejected(specs: Path) -> None:
    with pytest.raises(SpecParseError, match="could not parse"):
        load_spec(specs / "invalid" / "malformed.yaml")


def test_openapi_4_is_rejected() -> None:
    with pytest.raises(SpecParseError, match=r"only OpenAPI 3\.x"):
        parse_spec_text('openapi: "4.0.0"\npaths: {}')


def test_empty_document_is_rejected() -> None:
    with pytest.raises(SpecParseError, match="empty"):
        parse_spec_text("# just a comment\n")


def test_non_mapping_root_is_rejected() -> None:
    with pytest.raises(SpecParseError, match="must be a mapping"):
        parse_spec_text("- a\n- b\n")


def test_non_mapping_paths_is_rejected() -> None:
    with pytest.raises(SpecParseError, match="'paths' must be a mapping"):
        parse_spec_text('openapi: "3.0.3"\npaths: [a, b]')


def test_missing_paths_yields_no_operations() -> None:
    assert parse_spec_text('openapi: "3.0.3"\ninfo: {title: E, version: "1"}').operations == {}


# ------------------------------------------------------------------ safety ---


def test_billion_laughs_is_refused(specs: Path) -> None:
    """A tiny file whose aliases expand exponentially must not be constructed."""
    with pytest.raises(SpecParseError, match="alias expansion"):
        load_spec(specs / "invalid" / "billion_laughs.yaml")


def test_legitimate_anchors_still_load() -> None:
    """The alias guard must not reject ordinary anchor reuse."""
    spec = parse_spec_text("""
openapi: "3.0.3"
info: {title: Anchors, version: "1"}
x-common: &common {type: string}
paths:
  /a:
    get:
      parameters:
        - {name: p, in: query, schema: *common}
      responses: {}
""")
    parameter = spec.operations["GET /a"].parameters[("p", ParameterLocation.QUERY)]
    assert parameter.json_schema == {"type": "string"}


def test_external_refs_are_rejected_not_ignored(specs: Path) -> None:
    """Silently dropping an unresolvable ref would make different specs compare equal."""
    with pytest.raises(SpecParseError, match="external and remote"):
        load_spec(specs / "invalid" / "external_ref.yaml")


def test_missing_ref_target_is_rejected(specs: Path) -> None:
    with pytest.raises(SpecParseError, match=r"\$ref target does not exist"):
        load_spec(specs / "invalid" / "missing_ref.yaml")


def test_deep_ref_chain_is_rejected() -> None:
    links = "\n".join(
        f'    S{i}: {{$ref: "#/components/schemas/S{i + 1}"}}' for i in range(MAX_REF_DEPTH + 5)
    )
    document = f"""
openapi: "3.0.3"
info: {{title: Deep, version: "1"}}
paths:
  /x:
    get:
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema: {{$ref: "#/components/schemas/S0"}}
components:
  schemas:
{links}
    S{MAX_REF_DEPTH + 5}: {{type: string}}
"""
    with pytest.raises(SpecParseError, match="too deep"):
        parse_spec_text(document)


def test_recursive_schema_resolves_to_a_marker(specs: Path) -> None:
    """A self-referential schema must terminate, not recurse forever."""
    spec = load_spec(specs / "synthetic" / "recursive.yaml")
    schema = spec.operations["GET /tree"].responses["200"].content["application/json"]
    assert schema["properties"]["name"] == {"type": "string"}
    assert schema["properties"]["children"]["items"] == {
        CIRCULAR_REF_KEY: "#/components/schemas/Node"
    }


# ------------------------------------------------------------ normalisation ---


def test_ref_is_inlined() -> None:
    spec = parse_spec_text("""
openapi: "3.0.3"
info: {title: R, version: "1"}
paths:
  /x:
    post:
      requestBody:
        content:
          application/json:
            schema: {$ref: "#/components/schemas/Body"}
      responses: {}
components:
  schemas:
    Body: {type: object, properties: {a: {type: string}}}
""")
    body = spec.operations["POST /x"].request_body
    assert body is not None
    assert body.content["application/json"]["properties"]["a"] == {"type": "string"}


def test_ref_siblings_override_the_target() -> None:
    """OpenAPI 3.1 allows keys beside $ref; they win over the referenced target."""
    spec = parse_spec_text("""
openapi: "3.1.0"
info: {title: R, version: "1"}
paths:
  /x:
    get:
      parameters:
        - name: p
          in: query
          schema: {$ref: "#/components/schemas/S", description: overridden}
      responses: {}
components:
  schemas:
    S: {type: string, description: original}
""")
    schema = spec.operations["GET /x"].parameters[("p", ParameterLocation.QUERY)].json_schema
    assert schema == {"type": "string", "description": "overridden"}


def test_path_level_parameters_are_inherited() -> None:
    spec = parse_spec_text("""
openapi: "3.0.3"
info: {title: P, version: "1"}
paths:
  /x:
    parameters:
      - {name: shared, in: header, schema: {type: string}}
    get:
      parameters:
        - {name: own, in: query, schema: {type: string}}
      responses: {}
    post:
      responses: {}
""")
    assert {name for name, _ in spec.operations["GET /x"].parameters} == {"shared", "own"}
    assert {name for name, _ in spec.operations["POST /x"].parameters} == {"shared"}


def test_operation_parameters_override_path_level() -> None:
    spec = parse_spec_text("""
openapi: "3.0.3"
info: {title: P, version: "1"}
paths:
  /x:
    parameters:
      - {name: p, in: query, required: false, schema: {type: string}}
    get:
      parameters:
        - {name: p, in: query, required: true, schema: {type: string}}
      responses: {}
""")
    assert spec.operations["GET /x"].parameters[("p", ParameterLocation.QUERY)].required is True


def test_path_parameters_default_to_required() -> None:
    """OpenAPI requires it; assuming otherwise would invent an optional-to-required change."""
    spec = parse_spec_text("""
openapi: "3.0.3"
info: {title: P, version: "1"}
paths:
  /x/{id}:
    get:
      parameters:
        - {name: id, in: path, schema: {type: string}}
      responses: {}
""")
    assert spec.operations["GET /x/{id}"].parameters[("id", ParameterLocation.PATH)].required


def test_parameters_are_keyed_by_name_and_location() -> None:
    """The same name in two locations is two distinct parameters."""
    spec = parse_spec_text("""
openapi: "3.0.3"
info: {title: P, version: "1"}
paths:
  /x:
    get:
      parameters:
        - {name: token, in: query, schema: {type: string}}
        - {name: token, in: header, schema: {type: string}}
      responses: {}
""")
    assert len(spec.operations["GET /x"].parameters) == 2


def test_non_operation_keys_are_ignored() -> None:
    spec = parse_spec_text("""
openapi: "3.0.3"
info: {title: P, version: "1"}
paths:
  /x:
    summary: A path
    description: Not an operation
    servers: [{url: "https://example.test"}]
    x-internal: true
    get:
      responses: {}
""")
    assert set(spec.operations) == {"GET /x"}


def test_every_http_method_is_recognised() -> None:
    operations = "\n".join(f"    {method.value}:\n      responses: {{}}" for method in HttpMethod)
    spec = parse_spec_text(
        f'openapi: "3.0.3"\ninfo: {{title: M, version: "1"}}\npaths:\n  /x:\n{operations}'
    )
    assert len(spec.operations) == len(HttpMethod)
    assert spec.methods_for("/x") == set(HttpMethod)


def test_unknown_parameter_location_is_skipped() -> None:
    spec = parse_spec_text("""
openapi: "3.0.3"
info: {title: P, version: "1"}
paths:
  /x:
    get:
      parameters:
        - {name: a, in: body, schema: {type: string}}
        - {name: b, in: query, schema: {type: string}}
      responses: {}
""")
    assert {name for name, _ in spec.operations["GET /x"].parameters} == {"b"}


def test_malformed_parameter_list_is_rejected() -> None:
    with pytest.raises(SpecParseError, match="'parameters' must be a list"):
        parse_spec_text("""
openapi: "3.0.3"
info: {title: P, version: "1"}
paths:
  /x:
    get:
      parameters: {name: a}
      responses: {}
""")


def test_endpoint_label_format() -> None:
    spec = parse_spec_text(MINIMAL)
    assert spec.operations["GET /items"].endpoint == "GET /items"
    assert spec.paths == {"/items"}
