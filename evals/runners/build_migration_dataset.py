"""Generate the migration benchmark dataset.

Written as a script so the cases are reviewable side by side and easy to extend,
but the output is checked in: the dataset is data, not something regenerated on
every run.

Each case is deliberately a different *shape* of problem, not the same rename
wearing different names. The tags record which axis each one exercises so a
headline number can be broken apart by change kind and repository shape.
"""

import json
import shutil
from pathlib import Path

ROOT = Path("evals/datasets/migration")

SPEC_TEMPLATE = """openapi: "3.0.3"
info: {{title: Example API, version: "{version}"}}
paths:
{paths}"""


def chat_path(request_props: str, response_props: str = "") -> str:
    response = (
        f"""
              schema:
                type: object
                properties:
{response_props}"""
        if response_props
        else ""
    )
    return f"""  /v1/chat/completions:
    post:
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
{request_props}
      responses:
        '200':
          description: OK
          content:
            application/json:{response}
"""


def write(case_dir: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        target = case_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


PYPROJECT = """[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = []

[tool.hatch.build.targets.wheel]
packages = ["{package}"]
"""


CASES: list[dict] = []


def case(
    case_id: str,
    description: str,
    expectation: str,
    tags: list[str],
    rationale: str,
    old_props: str,
    new_props: str,
    files: dict[str, str],
    hidden: dict[str, str],
    packages: list[str] | None = None,
    old_response: str = "",
    new_response: str = "",
) -> None:
    CASES.append(
        {
            "case_id": case_id,
            "description": description,
            "expectation": expectation,
            "tags": tags,
            "rationale": rationale,
            "old_props": old_props,
            "new_props": new_props,
            "old_response": old_response,
            "new_response": new_response,
            "files": files,
            "hidden": hidden,
            "packages": packages or [],
        }
    )


# ---------------------------------------------------------------- 01 rename ---
case(
    "01-request-field-renamed",
    "A request field is renamed; one call site builds the payload as a dict.",
    "migrate",
    ["change:field-renamed", "shape:single-module"],
    "The old key no longer exists in the request schema, so a payload using it "
    "would be rejected by the server.",
    old_props="                max_tokens: {type: integer}\n                model: {type: string}",
    new_props="                max_completion_tokens: {type: integer}\n                model: {type: string}",
    files={
        "repo/pyproject.toml": PYPROJECT.format(name="chatapp", package="chatapp"),
        "repo/chatapp/__init__.py": '''"""Build requests for the chat completions endpoint."""


def build_payload(prompt: str, limit: int = 256) -> dict[str, object]:
    """Build the JSON body sent to /v1/chat/completions."""
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": limit,
    }
''',
        "repo/tests/test_payload.py": """from chatapp import build_payload


def test_payload_carries_the_model():
    assert build_payload("hi")["model"] == "gpt-4o"


def test_payload_carries_the_limit():
    assert build_payload("hi", 8)["max_tokens"] == 8
""",
    },
    hidden={
        "tests/test_contract.py": '''"""Injected at grading time; the agent never sees this file."""

from chatapp import build_payload

ALLOWED = {"model", "messages", "max_completion_tokens"}


def test_payload_uses_only_fields_the_new_api_accepts():
    assert set(build_payload("hi")) <= ALLOWED


def test_the_limit_is_sent_under_the_new_name():
    assert build_payload("hi", 8)["max_completion_tokens"] == 8
''',
    },
)

# ------------------------------------------------------- 02 spread call site ---
case(
    "02-rename-across-modules",
    "The same renamed field is used in three modules, one of them not obviously an API call.",
    "migrate",
    ["change:field-renamed", "shape:multi-module", "difficulty:spread"],
    "Every construction and every read of the old key has to move, including the "
    "helper that post-processes an already-built payload.",
    old_props="                max_tokens: {type: integer}\n                model: {type: string}",
    new_props="                max_completion_tokens: {type: integer}\n                model: {type: string}",
    files={
        "repo/pyproject.toml": PYPROJECT.format(name="spread", package="spread"),
        "repo/spread/__init__.py": '''"""Request construction."""

DEFAULT_LIMIT = 256


def build_payload(prompt: str, limit: int = DEFAULT_LIMIT) -> dict[str, object]:
    """Build the JSON body."""
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": limit,
    }
''',
        "repo/spread/budget.py": '''"""Cost controls applied to an already-built payload."""

from typing import Any

HARD_CAP = 1024


def clamp(payload: dict[str, Any]) -> dict[str, Any]:
    """Lower the request's token limit to the hard cap."""
    capped = dict(payload)
    capped["max_tokens"] = min(int(capped.get("max_tokens", 0)), HARD_CAP)
    return capped
''',
        "repo/spread/logging_helpers.py": '''"""Structured logging for outgoing requests."""

from typing import Any


def describe(payload: dict[str, Any]) -> str:
    """One-line description of a request, for the log."""
    return f"model={payload['model']} limit={payload['max_tokens']}"
''',
        "repo/tests/test_spread.py": """from spread import build_payload
from spread.budget import clamp
from spread.logging_helpers import describe


def test_clamp_lowers_an_oversized_limit():
    assert clamp(build_payload("hi", 99999))["max_tokens"] == 1024


def test_describe_mentions_the_limit():
    assert "limit=8" in describe(build_payload("hi", 8))
""",
    },
    hidden={
        "tests/test_contract.py": """from spread import build_payload
from spread.budget import clamp
from spread.logging_helpers import describe

ALLOWED = {"model", "messages", "max_completion_tokens"}


def test_payload_uses_only_new_fields():
    assert set(build_payload("hi")) <= ALLOWED


def test_clamp_operates_on_the_new_field():
    assert clamp(build_payload("hi", 99999))["max_completion_tokens"] == 1024


def test_describe_still_reports_the_limit():
    assert "limit=8" in describe(build_payload("hi", 8))
""",
    },
)

# --------------------------------------------------------- 03 field removed ---
case(
    "03-request-field-removed",
    "A request field is removed outright, with no replacement.",
    "migrate",
    ["change:field-removed", "shape:single-module"],
    "The field is gone from the schema and has no successor, so the correct "
    "migration is to stop sending it rather than to rename it.",
    old_props="                model: {type: string}\n                best_of: {type: integer}",
    new_props="                model: {type: string}",
    files={
        "repo/pyproject.toml": PYPROJECT.format(name="removal", package="removal"),
        "repo/removal/__init__.py": '''"""Request construction."""


def build_payload(prompt: str) -> dict[str, object]:
    """Build the JSON body."""
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "best_of": 3,
    }
''',
        "repo/tests/test_payload.py": """from removal import build_payload


def test_payload_names_the_model():
    assert build_payload("hi")["model"] == "gpt-4o"
""",
    },
    hidden={
        "tests/test_contract.py": """from removal import build_payload

ALLOWED = {"model", "messages"}


def test_the_removed_field_is_no_longer_sent():
    assert "best_of" not in build_payload("hi")


def test_nothing_else_was_dropped():
    assert set(build_payload("hi")) == ALLOWED
""",
    },
)

# ------------------------------------------------------- 04 response field ---
case(
    "04-response-field-renamed",
    "A response field is renamed; the code reads it rather than writing it.",
    "migrate",
    ["change:response-renamed", "shape:single-module", "difficulty:direction"],
    "A response field is read, not sent. The migration has to change the reader, "
    "and must not touch the request payload that happens to share vocabulary.",
    old_props="                model: {type: string}",
    new_props="                model: {type: string}",
    old_response="                    finish_reason: {type: string}\n"
    "                    usage: {type: object}",
    new_response="                    stop_reason: {type: string}\n"
    "                    usage: {type: object}",
    files={
        "repo/pyproject.toml": PYPROJECT.format(name="reader", package="reader"),
        "repo/reader/__init__.py": '''"""Interpret a chat completion response."""

from typing import Any


def build_payload(prompt: str) -> dict[str, object]:
    """Build the JSON body. Unrelated to the response change."""
    return {"model": "gpt-4o", "messages": [{"role": "user", "content": prompt}]}


def was_truncated(response: dict[str, Any]) -> bool:
    """Whether the model stopped because it ran out of room."""
    return response["finish_reason"] == "length"
''',
        "repo/tests/test_reader.py": """from reader import build_payload, was_truncated


def test_truncation_is_detected():
    assert was_truncated({"finish_reason": "length"})


def test_payload_names_the_model():
    assert build_payload("hi")["model"] == "gpt-4o"
""",
    },
    hidden={
        "tests/test_contract.py": """from reader import build_payload, was_truncated


def test_the_reader_uses_the_new_response_field():
    assert was_truncated({"stop_reason": "length"})
    assert not was_truncated({"stop_reason": "stop"})


def test_the_request_payload_was_left_alone():
    assert set(build_payload("hi")) == {"model", "messages"}
""",
    },
)

# ----------------------------------------------------- 05 enum value removed ---
case(
    "05-enum-value-removed",
    "An accepted enum value is removed and replaced by a new one.",
    "migrate",
    ["change:enum-removed", "shape:single-module"],
    "The literal string is no longer accepted by the server, so any code that "
    "sends it has to send the replacement instead.",
    old_props="                model: {type: string}\n"
    "                response_format: {type: string, enum: [text, json, srt]}",
    new_props="                model: {type: string}\n"
    "                response_format: {type: string, enum: [text, json_object, srt]}",
    files={
        "repo/pyproject.toml": PYPROJECT.format(name="formats", package="formats"),
        "repo/formats/__init__.py": '''"""Choose an output format for a request."""


def build_payload(prompt: str, structured: bool = False) -> dict[str, object]:
    """Build the JSON body, asking for JSON when structured output is wanted."""
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": "json" if structured else "text",
    }
''',
        "repo/tests/test_formats.py": """from formats import build_payload


def test_plain_requests_ask_for_text():
    assert build_payload("hi")["response_format"] == "text"
""",
    },
    hidden={
        "tests/test_contract.py": """from formats import build_payload

ACCEPTED = {"text", "json_object", "srt"}


def test_structured_requests_use_an_accepted_value():
    assert build_payload("hi", structured=True)["response_format"] in ACCEPTED


def test_plain_requests_still_ask_for_text():
    assert build_payload("hi")["response_format"] == "text"
""",
    },
)

# --------------------------------------------------------- 06 raw http client ---
case(
    "06-raw-http-client",
    "No SDK: the request is built and posted with a hand-rolled HTTP helper.",
    "migrate",
    ["change:field-renamed", "shape:raw-http"],
    "The field name appears only inside a dict literal passed to a generic post "
    "helper, so nothing about the call site names the API.",
    old_props="                model: {type: string}\n                max_tokens: {type: integer}",
    new_props="                model: {type: string}\n"
    "                max_completion_tokens: {type: integer}",
    files={
        "repo/pyproject.toml": PYPROJECT.format(name="rawhttp", package="rawhttp"),
        "repo/rawhttp/__init__.py": '''"""A deliberately generic HTTP layer."""

from typing import Any

BASE_URL = "https://api.example.com"


def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Pretend to send a request. Returns the body it would have sent."""
    return {"url": BASE_URL + path, "json": body}


def complete(prompt: str, limit: int = 128) -> dict[str, Any]:
    """Ask the chat endpoint for a completion."""
    return post(
        "/v1/chat/completions",
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": limit,
        },
    )
''',
        "repo/tests/test_rawhttp.py": """from rawhttp import complete


def test_the_request_targets_the_chat_endpoint():
    assert complete("hi")["url"].endswith("/v1/chat/completions")
""",
    },
    hidden={
        "tests/test_contract.py": """from rawhttp import complete

ALLOWED = {"model", "messages", "max_completion_tokens"}


def test_the_body_uses_only_accepted_fields():
    assert set(complete("hi")["json"]) <= ALLOWED


def test_the_limit_survives_the_rename():
    assert complete("hi", 8)["json"]["max_completion_tokens"] == 8
""",
    },
)

# ----------------------------------------------------- 07 required field added ---
case(
    "07-required-field-added",
    "A previously optional field becomes required.",
    "migrate",
    ["change:required-added", "shape:single-module", "limitation:nothing-to-match"],
    "A request that omits the now-required field is rejected, so the migration "
    "has to start sending it. Kept deliberately as a known limitation: impact "
    "analysis locates affected code by matching names that appear in it, and a "
    "field the repository has never sent appears nowhere, so there is no anchor "
    "to find. Rewire is expected to fail this case today; it is in the dataset "
    "so that stops being invisible.",
    old_props="                model: {type: string}\n                stream: {type: boolean}",
    new_props="                model: {type: string}\n                stream: {type: boolean}",
    files={
        "repo/pyproject.toml": PYPROJECT.format(name="required", package="required"),
        "repo/required/__init__.py": '''"""Request construction."""


def build_payload(prompt: str) -> dict[str, object]:
    """Build the JSON body."""
    return {"model": "gpt-4o", "messages": [{"role": "user", "content": prompt}]}
''',
        "repo/tests/test_required.py": """from required import build_payload


def test_payload_names_the_model():
    assert build_payload("hi")["model"] == "gpt-4o"
""",
    },
    hidden={
        "tests/test_contract.py": """from required import build_payload


def test_the_now_required_field_is_sent():
    assert "stream" in build_payload("hi")
""",
    },
)

# ------------------------------------------------------------- 08 wrapper layer ---
case(
    "08-wrapper-and-tests",
    "A wrapper function whose own parameter shares the API field's name.",
    "migrate",
    ["change:field-renamed", "shape:wrapper", "difficulty:name-collision"],
    "The wire field is renamed but the wrapper's Python parameter is not part of "
    "the API. Renaming the parameter would be a gratuitous public-API break.",
    old_props="                model: {type: string}\n                max_tokens: {type: integer}",
    new_props="                model: {type: string}\n"
    "                max_completion_tokens: {type: integer}",
    files={
        "repo/pyproject.toml": PYPROJECT.format(name="wrapper", package="wrapper"),
        "repo/wrapper/__init__.py": '''"""A thin wrapper over the chat endpoint."""


def complete(prompt: str, max_tokens: int = 64) -> dict[str, object]:
    """Send a completion request.

    ``max_tokens`` is this function's own public parameter.
    """
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
''',
        "repo/tests/test_wrapper.py": """from wrapper import complete


def test_the_wrapper_accepts_a_limit():
    assert complete("hi", max_tokens=8)["max_tokens"] == 8
""",
    },
    hidden={
        "tests/test_contract.py": """import inspect

from wrapper import complete


def test_the_wire_field_was_renamed():
    assert complete("hi", 8)["max_completion_tokens"] == 8


def test_the_public_python_parameter_was_not_renamed():
    assert "max_tokens" in inspect.signature(complete).parameters
""",
    },
)

# ------------------------------------------------------------- 09 no-op case ---
case(
    "09-unrelated-change",
    "The specification changes an endpoint this repository never calls.",
    "no_op",
    ["change:unrelated", "shape:negative"],
    "Nothing in this repository touches the changed endpoint. Producing a patch "
    "here is a false positive, and is the failure mode this case exists to catch.",
    old_props="                model: {type: string}",
    new_props="                model: {type: string}",
    files={
        "repo/pyproject.toml": PYPROJECT.format(name="unrelated", package="unrelated"),
        "repo/unrelated/__init__.py": '''"""This package only ever calls the embeddings endpoint."""


def build_embedding_request(text: str) -> dict[str, object]:
    """Build the JSON body for /v1/embeddings."""
    return {"model": "text-embedding-3-small", "input": text}
''',
        "repo/tests/test_unrelated.py": """from unrelated import build_embedding_request


def test_the_request_names_the_embedding_model():
    assert build_embedding_request("hi")["model"] == "text-embedding-3-small"
""",
    },
    hidden={
        "tests/test_contract.py": """from unrelated import build_embedding_request


def test_the_embedding_request_is_unchanged():
    assert build_embedding_request("hi") == {
        "model": "text-embedding-3-small",
        "input": "hi",
    }
""",
    },
)

# ------------------------------------------------------- 10 partially migrated ---
case(
    "10-partially-migrated",
    "One call site was already updated by hand; the rest were not.",
    "migrate",
    ["change:field-renamed", "shape:multi-module", "difficulty:partial"],
    "Half the repository already uses the new name. A correct migration finishes "
    "the job without re-breaking the part that is already right.",
    old_props="                model: {type: string}\n                max_tokens: {type: integer}",
    new_props="                model: {type: string}\n"
    "                max_completion_tokens: {type: integer}",
    files={
        "repo/pyproject.toml": PYPROJECT.format(name="partial", package="partial"),
        "repo/partial/__init__.py": '''"""Already migrated by hand."""


def build_chat_payload(prompt: str, limit: int = 64) -> dict[str, object]:
    """Build the JSON body. This one is already correct."""
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": limit,
    }
''',
        "repo/partial/batch.py": '''"""Not yet migrated."""


def build_batch_payload(prompts: list[str], limit: int = 64) -> dict[str, object]:
    """Build the JSON body for a batch of prompts."""
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": p} for p in prompts],
        "max_tokens": limit,
    }
''',
        "repo/tests/test_partial.py": """from partial import build_chat_payload
from partial.batch import build_batch_payload


def test_chat_payload_is_already_migrated():
    assert "max_completion_tokens" in build_chat_payload("hi")


def test_batch_payload_carries_a_limit():
    assert build_batch_payload(["hi"])["max_tokens"] == 64
""",
    },
    hidden={
        "tests/test_contract.py": """from partial import build_chat_payload
from partial.batch import build_batch_payload

ALLOWED = {"model", "messages", "max_completion_tokens"}


def test_the_already_migrated_call_site_still_works():
    assert set(build_chat_payload("hi")) <= ALLOWED
    assert build_chat_payload("hi", 8)["max_completion_tokens"] == 8


def test_the_remaining_call_site_was_finished():
    assert set(build_batch_payload(["hi"])) <= ALLOWED
    assert build_batch_payload(["hi"], 8)["max_completion_tokens"] == 8
""",
    },
)


def main() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    for entry in CASES:
        directory = ROOT / entry["case_id"]
        directory.mkdir(parents=True)
        write(directory, entry["files"])
        write(directory, {f"hidden/{k}": v for k, v in entry["hidden"].items()})

        for name, props, response in (
            ("old.yaml", entry["old_props"], entry["old_response"]),
            ("new.yaml", entry["new_props"], entry["new_response"]),
        ):
            paths = chat_path(props, response)
            if entry["case_id"] == "07-required-field-added" and name == "new.yaml":
                paths = paths.replace(
                    "              properties:\n",
                    "              required: [stream]\n              properties:\n",
                    1,
                )
            if entry["case_id"] == "09-unrelated-change":
                extra = (
                    "  /v1/audio/speech:\n    post:\n      requestBody:\n        content:\n"
                    "          application/json:\n            schema:\n              type: object\n"
                    "              properties:\n"
                    f"                {'voice' if name == 'old.yaml' else 'voice_id'}: "
                    "{type: string}\n"
                    "      responses:\n        '200': {description: OK}\n"
                )
                paths = paths + extra
            (directory / name).write_text(
                SPEC_TEMPLATE.format(version="1" if name == "old.yaml" else "2", paths=paths),
                encoding="utf-8",
            )

        manifest = {
            "case_id": entry["case_id"],
            "description": entry["description"],
            "expectation": entry["expectation"],
            "packages": entry["packages"],
            "tags": entry["tags"],
            "rationale": entry["rationale"],
        }
        (directory / "case.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    print(f"wrote {len(CASES)} cases to {ROOT}")


main()
