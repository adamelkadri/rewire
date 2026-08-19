"""Loading and normalising OpenAPI specifications.

An OpenAPI document is not a convenient shape to diff. The same operation can be
described in several equivalent ways: parameters may be declared on the path item
or on the operation, schemas may be inlined or hidden behind arbitrarily deep
``$ref`` chains, and 3.0's ``nullable: true`` means what 3.1 writes as a union
with ``null``.

This module collapses those variations into a normalised :class:`ApiSpec` so that
the differ can compare two documents structurally rather than textually. Anything
that survives normalisation is a real difference.

Specifications are untrusted input, so loading is defensive: the file size is
capped, YAML is parsed with ``SafeLoader``, alias expansion is bounded before any
object is constructed (see :func:`_guard_alias_expansion`), and ``$ref`` cycles
resolve to an opaque marker instead of recursing forever.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

from rewire.core.errors import SpecParseError

#: Largest specification Rewire will read. Real-world specs are far smaller
#: (Stripe's, one of the largest published, is a few megabytes).
MAX_SPEC_BYTES: Final[int] = 32 * 1024 * 1024

#: Ceiling on nodes produced by YAML alias expansion. A "billion laughs" payload
#: is tiny on disk but expands exponentially, so the size cap alone is not a
#: defence; this bounds the expansion itself.
MAX_YAML_NODES: Final[int] = 5_000_000

#: How deep ``$ref`` chains may nest before the document is rejected.
MAX_REF_DEPTH: Final[int] = 100

#: Marker substituted for a ``$ref`` that points back into its own resolution
#: chain. The differ compares these by target, never by structure.
CIRCULAR_REF_KEY: Final[str] = "$circularRef"


class HttpMethod(StrEnum):
    """HTTP methods that OpenAPI treats as operations on a path item."""

    GET = "get"
    PUT = "put"
    POST = "post"
    DELETE = "delete"
    OPTIONS = "options"
    HEAD = "head"
    PATCH = "patch"
    TRACE = "trace"


class ParameterLocation(StrEnum):
    """Where a parameter is transmitted."""

    QUERY = "query"
    HEADER = "header"
    PATH = "path"
    COOKIE = "cookie"


#: JSON Schema is deliberately kept as a plain mapping rather than modelled in
#: Pydantic. The dialect is large, extensible and vendor-extended in practice;
#: a partial model would silently drop keywords, and dropping keywords in a
#: breaking-change detector means missing breaking changes.
JsonSchema = dict[str, Any]


class Parameter(BaseModel):
    """A normalised operation parameter."""

    model_config = ConfigDict(frozen=True)

    name: str
    location: ParameterLocation
    required: bool = False
    deprecated: bool = False
    json_schema: JsonSchema = Field(default_factory=dict)

    @property
    def key(self) -> tuple[str, ParameterLocation]:
        """Identity of a parameter: OpenAPI keys parameters by name *and* location."""
        return (self.name, self.location)


class Body(BaseModel):
    """A request body or a single response, keyed by media type."""

    model_config = ConfigDict(frozen=True)

    required: bool = False
    content: dict[str, JsonSchema] = Field(default_factory=dict)


class Operation(BaseModel):
    """A normalised single operation: one method on one path."""

    model_config = ConfigDict(frozen=True)

    path: str
    method: HttpMethod
    operation_id: str | None = None
    summary: str | None = None
    deprecated: bool = False
    parameters: dict[tuple[str, ParameterLocation], Parameter] = Field(default_factory=dict)
    request_body: Body | None = None
    responses: dict[str, Body] = Field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        """Human- and machine-stable label, e.g. ``POST /v1/messages``."""
        return f"{self.method.value.upper()} {self.path}"


class SpecMetadata(BaseModel):
    """Identifying information about a loaded specification."""

    model_config = ConfigDict(frozen=True)

    title: str | None = None
    version: str | None = None
    openapi_version: str | None = None
    source: str | None = None


class ApiSpec(BaseModel):
    """A normalised OpenAPI document."""

    model_config = ConfigDict(frozen=True)

    metadata: SpecMetadata = Field(default_factory=SpecMetadata)
    operations: dict[str, Operation] = Field(default_factory=dict)

    @property
    def paths(self) -> set[str]:
        """Every path the specification declares an operation on."""
        return {operation.path for operation in self.operations.values()}

    def methods_for(self, path: str) -> set[HttpMethod]:
        """Return the methods declared on ``path``."""
        return {op.method for op in self.operations.values() if op.path == path}


# --------------------------------------------------------------------- load ---


def _guard_alias_expansion(node: yaml.Node, limit: int) -> None:
    """Reject documents whose YAML aliases expand beyond ``limit`` nodes.

    ``SafeLoader`` is safe against arbitrary object construction but not against
    exponential alias expansion. Composing the node graph first is what makes the
    check possible: in the graph an alias is a shared reference, so the expanded
    size can be computed by memoised recursion without ever materialising it.
    """
    sizes: dict[int, int] = {}

    def expanded_size(current: yaml.Node) -> int:
        cached = sizes.get(id(current))
        if cached is not None:
            return cached

        total = 1
        if isinstance(current, yaml.SequenceNode):
            for item in current.value:
                total += expanded_size(item)
        elif isinstance(current, yaml.MappingNode):
            for key, value in current.value:
                total += expanded_size(key) + expanded_size(value)

        if total > limit:
            raise SpecParseError(
                "YAML alias expansion exceeds the safety limit",
                limit=limit,
            )
        sizes[id(current)] = total
        return total

    expanded_size(node)


def _parse_yaml(text: str, *, source: str) -> Any:
    """Parse YAML (and therefore JSON) safely, with bounded alias expansion."""
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            raise SpecParseError("specification is empty", source=source)
        _guard_alias_expansion(node, MAX_YAML_NODES)
        return loader.construct_document(node)
    except yaml.YAMLError as exc:
        raise SpecParseError(f"could not parse specification: {exc}", source=source) from exc
    finally:
        loader.dispose()


def parse_spec_text(text: str, *, source: str = "<string>") -> ApiSpec:
    """Parse and normalise a specification from a string.

    Args:
        text: Raw YAML or JSON. YAML is a superset of JSON, so one parser covers
            both; JSON is tried first only because its errors point at the exact
            offset.
        source: Label used in error messages and recorded in the metadata.

    Raises:
        SpecParseError: The document is unparseable, empty, not a mapping, or not
            an OpenAPI 3.x specification.
    """
    try:
        document = json.loads(text)
    except ValueError:
        document = _parse_yaml(text, source=source)

    if not isinstance(document, dict):
        raise SpecParseError(
            "specification root must be a mapping",
            source=source,
            found=type(document).__name__,
        )
    return normalise_document(document, source=source)


def load_spec(path: Path | str, *, max_bytes: int = MAX_SPEC_BYTES) -> ApiSpec:
    """Load and normalise a specification from disk.

    Args:
        path: Path to a ``.yaml``, ``.yml`` or ``.json`` OpenAPI 3.x document.
        max_bytes: Refuse files larger than this. Specifications are untrusted
            input and are read fully into memory.

    Raises:
        SpecParseError: The file is missing, unreadable, oversized or invalid.
    """
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise SpecParseError(f"could not read specification: {exc}", path=str(file_path)) from exc

    if size > max_bytes:
        raise SpecParseError(
            "specification is larger than the safety limit",
            path=str(file_path),
            size_bytes=size,
            limit_bytes=max_bytes,
        )

    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SpecParseError(f"could not read specification: {exc}", path=str(file_path)) from exc

    return parse_spec_text(text, source=str(file_path))


# ---------------------------------------------------------------- normalise ---


class _RefResolver:
    """Resolves internal ``$ref`` pointers, inlining them into plain schemas.

    Only same-document refs (``#/...``) are supported. External and remote refs
    are rejected rather than silently ignored: quietly treating an unresolvable
    ``$ref`` as an empty schema would make two different documents compare equal,
    which is the one failure mode a breaking-change detector must not have.
    """

    def __init__(self, document: dict[str, Any]) -> None:
        self._document = document

    def resolve(self, value: Any, *, stack: tuple[str, ...] = ()) -> Any:
        """Return ``value`` with every internal ``$ref`` replaced by its target."""
        if isinstance(value, list):
            return [self.resolve(item, stack=stack) for item in value]
        if not isinstance(value, dict):
            return value

        ref = value.get("$ref")
        if isinstance(ref, str):
            return self._resolve_ref(ref, value, stack=stack)

        return {key: self.resolve(item, stack=stack) for key, item in value.items()}

    def _resolve_ref(self, ref: str, node: dict[str, Any], *, stack: tuple[str, ...]) -> Any:
        if not ref.startswith("#/"):
            raise SpecParseError(
                "external and remote $ref pointers are not supported",
                ref=ref,
            )
        if ref in stack:
            # A self-referential schema (a tree node containing children of its
            # own type). Inlining would not terminate, so record the target and
            # let the differ compare markers by name.
            return {CIRCULAR_REF_KEY: ref}
        if len(stack) >= MAX_REF_DEPTH:
            raise SpecParseError("$ref chain is too deep", ref=ref, limit=MAX_REF_DEPTH)

        target = self._lookup(ref)
        resolved = self.resolve(target, stack=(*stack, ref))

        # Sibling keys alongside a $ref (permitted in OpenAPI 3.1) override the
        # target, so the target is merged underneath them.
        siblings = {
            key: self.resolve(item, stack=stack) for key, item in node.items() if key != "$ref"
        }
        if siblings and isinstance(resolved, dict):
            return {**resolved, **siblings}
        return resolved

    def _lookup(self, ref: str) -> Any:
        current: Any = self._document
        for raw_token in ref[2:].split("/"):
            token = raw_token.replace("~1", "/").replace("~0", "~")
            if not isinstance(current, dict) or token not in current:
                raise SpecParseError("$ref target does not exist", ref=ref)
            current = current[token]
        return current


def _require_openapi_3(document: dict[str, Any], *, source: str) -> str:
    version = document.get("openapi")
    if version is None:
        if "swagger" in document:
            raise SpecParseError(
                "Swagger 2.0 documents are not supported; convert to OpenAPI 3.x first",
                source=source,
                swagger=str(document["swagger"]),
            )
        raise SpecParseError(
            "document is not an OpenAPI specification (no 'openapi' field)",
            source=source,
        )
    if not isinstance(version, str) or not version.startswith("3."):
        raise SpecParseError(
            "only OpenAPI 3.x specifications are supported",
            source=source,
            openapi=str(version),
        )
    return version


def _normalise_parameters(
    raw_parameters: Any, resolver: _RefResolver, *, source: str
) -> dict[tuple[str, ParameterLocation], Parameter]:
    if raw_parameters is None:
        return {}
    if not isinstance(raw_parameters, list):
        raise SpecParseError("'parameters' must be a list", source=source)

    parameters: dict[tuple[str, ParameterLocation], Parameter] = {}
    for entry in raw_parameters:
        resolved = resolver.resolve(entry)
        if not isinstance(resolved, dict):
            continue
        name = resolved.get("name")
        location = resolved.get("in")
        if not isinstance(name, str) or not isinstance(location, str):
            continue
        try:
            parsed_location = ParameterLocation(location)
        except ValueError:
            continue  # Unknown location; not something a client migration acts on.

        parameter = Parameter(
            name=name,
            location=parsed_location,
            # OpenAPI requires path parameters to be required; treat them as such
            # even when a document omits the flag, so that an omission does not
            # read as an optional-to-required change.
            required=bool(resolved.get("required", parsed_location is ParameterLocation.PATH)),
            deprecated=bool(resolved.get("deprecated", False)),
            json_schema=resolved.get("schema") or {},
        )
        parameters[parameter.key] = parameter
    return parameters


def _normalise_content(raw_content: Any, resolver: _RefResolver) -> dict[str, JsonSchema]:
    if not isinstance(raw_content, dict):
        return {}
    content: dict[str, JsonSchema] = {}
    for media_type, media in raw_content.items():
        if not isinstance(media_type, str):
            continue
        resolved = resolver.resolve(media)
        schema = resolved.get("schema") if isinstance(resolved, dict) else None
        content[media_type] = schema if isinstance(schema, dict) else {}
    return content


def _normalise_request_body(raw_body: Any, resolver: _RefResolver) -> Body | None:
    if raw_body is None:
        return None
    resolved = resolver.resolve(raw_body)
    if not isinstance(resolved, dict):
        return None
    return Body(
        required=bool(resolved.get("required", False)),
        content=_normalise_content(resolved.get("content"), resolver),
    )


def _normalise_responses(raw_responses: Any, resolver: _RefResolver) -> dict[str, Body]:
    if not isinstance(raw_responses, dict):
        return {}
    responses: dict[str, Body] = {}
    for status_code, raw_response in raw_responses.items():
        resolved = resolver.resolve(raw_response)
        if not isinstance(resolved, dict):
            continue
        responses[str(status_code)] = Body(
            required=True,
            content=_normalise_content(resolved.get("content"), resolver),
        )
    return responses


def normalise_document(document: dict[str, Any], *, source: str = "<string>") -> ApiSpec:
    """Normalise a parsed OpenAPI 3.x document into an :class:`ApiSpec`.

    Path-level parameters are merged into every operation on that path, with
    operation-level declarations taking precedence, so that two documents that
    place the same parameter at different levels compare as equal.
    """
    openapi_version = _require_openapi_3(document, source=source)
    resolver = _RefResolver(document)

    raw_info = document.get("info")
    info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    title = info.get("title")
    version = info.get("version")
    metadata = SpecMetadata(
        title=title if isinstance(title, str) else None,
        version=str(version) if version is not None else None,
        openapi_version=openapi_version,
        source=source,
    )

    raw_paths = document.get("paths")
    if raw_paths is None:
        return ApiSpec(metadata=metadata, operations={})
    if not isinstance(raw_paths, dict):
        raise SpecParseError("'paths' must be a mapping", source=source)

    operations: dict[str, Operation] = {}
    for path, raw_item in raw_paths.items():
        if not isinstance(raw_item, dict):
            continue
        path_item = resolver.resolve(raw_item)
        shared = _normalise_parameters(path_item.get("parameters"), resolver, source=source)

        for method_name, raw_operation in path_item.items():
            try:
                method = HttpMethod(str(method_name).lower())
            except ValueError:
                continue  # 'summary', 'parameters', 'servers', extensions, ...
            if not isinstance(raw_operation, dict):
                continue

            own = _normalise_parameters(raw_operation.get("parameters"), resolver, source=source)
            operation = Operation(
                path=str(path),
                method=method,
                operation_id=(
                    raw_operation["operationId"]
                    if isinstance(raw_operation.get("operationId"), str)
                    else None
                ),
                summary=(
                    raw_operation["summary"]
                    if isinstance(raw_operation.get("summary"), str)
                    else None
                ),
                deprecated=bool(raw_operation.get("deprecated", False)),
                parameters={**shared, **own},
                request_body=_normalise_request_body(raw_operation.get("requestBody"), resolver),
                responses=_normalise_responses(raw_operation.get("responses"), resolver),
            )
            operations[operation.endpoint] = operation

    return ApiSpec(metadata=metadata, operations=operations)


__all__ = [
    "CIRCULAR_REF_KEY",
    "MAX_REF_DEPTH",
    "MAX_SPEC_BYTES",
    "MAX_YAML_NODES",
    "ApiSpec",
    "Body",
    "HttpMethod",
    "JsonSchema",
    "Operation",
    "Parameter",
    "ParameterLocation",
    "SpecMetadata",
    "load_spec",
    "normalise_document",
    "parse_spec_text",
]
