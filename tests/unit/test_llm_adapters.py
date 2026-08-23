"""Tests for the Anthropic and OpenAI adapters.

No network. Each adapter's translation functions are pure, and the request path
is exercised against a stubbed SDK client, so these tests run in CI without a
key and without a bill.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import openai
import pytest

from rewire.core.errors import LLMError, LLMRateLimitError
from rewire.llm.anthropic_provider import (
    AnthropicProvider,
    from_anthropic_response,
    to_anthropic_messages,
    to_anthropic_tool,
)
from rewire.llm.models import Message, StopReason, ToolCall, ToolSpec
from rewire.llm.openai_provider import (
    OpenAIProvider,
    from_openai_response,
    to_openai_messages,
    to_openai_tool,
)


def http_response(status: int) -> httpx2.Response:
    """Build a real transport response; the SDK error types require one."""
    return httpx2.Response(status, request=httpx2.Request("POST", "https://api.test/v1"))


SPEC = ToolSpec(
    name="read_file",
    description="Read a file.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
)
CALL = ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})


# --------------------------------------------------------------- anthropic ---


def test_anthropic_tool_uses_input_schema() -> None:
    rendered = to_anthropic_tool(SPEC)
    assert rendered["name"] == "read_file"
    assert rendered["input_schema"]["properties"]["path"]["type"] == "string"


def test_anthropic_assistant_tool_calls_become_blocks() -> None:
    rendered = to_anthropic_messages([Message.assistant("thinking", (CALL,))])
    blocks = rendered[0]["content"]
    assert blocks[0] == {"type": "text", "text": "thinking"}
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["input"] == {"path": "a.py"}


def test_anthropic_merges_consecutive_tool_results() -> None:
    """Splitting parallel results across turns trains the model out of parallel calls."""
    other = ToolCall(id="call_2", name="read_file", arguments={})
    rendered = to_anthropic_messages(
        [Message.tool_result(CALL, "one"), Message.tool_result(other, "two")]
    )
    assert len(rendered) == 1
    assert [block["tool_use_id"] for block in rendered[0]["content"]] == ["call_1", "call_2"]


def test_anthropic_marks_tool_errors() -> None:
    rendered = to_anthropic_messages([Message.tool_result(CALL, "boom", is_error=True)])
    assert rendered[0]["content"][0]["is_error"] is True


def test_anthropic_tool_result_after_assistant_starts_a_new_turn() -> None:
    rendered = to_anthropic_messages(
        [Message.assistant("x", (CALL,)), Message.tool_result(CALL, "ok")]
    )
    assert [entry["role"] for entry in rendered] == ["assistant", "user"]


def fake_anthropic_response(**overrides: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "content": [
            SimpleNamespace(type="text", text="Hello"),
            SimpleNamespace(type="tool_use", id="call_1", name="read_file", input={"path": "a.py"}),
        ],
        "stop_reason": "tool_use",
        "model": "claude-opus-5",
        "usage": SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=5,
            cache_creation_input_tokens=3,
        ),
    }
    return SimpleNamespace(**{**defaults, **overrides})


def test_anthropic_response_normalisation() -> None:
    response = from_anthropic_response(fake_anthropic_response())
    assert response.text == "Hello"
    assert response.stop_reason is StopReason.TOOL_USE
    assert response.tool_calls[0].arguments == {"path": "a.py"}
    assert response.usage.input_tokens == 100
    assert response.usage.cache_read_tokens == 5
    assert response.usage.cache_write_tokens == 3


def test_anthropic_unknown_stop_reason_maps_to_other() -> None:
    response = from_anthropic_response(fake_anthropic_response(stop_reason="something_new"))
    assert response.stop_reason is StopReason.OTHER


def test_anthropic_thinking_blocks_are_ignored() -> None:
    """Thinking is not part of the neutral model and must not leak into text."""
    raw = fake_anthropic_response(
        content=[
            SimpleNamespace(type="thinking", thinking="internal"),
            SimpleNamespace(type="text", text="answer"),
        ]
    )
    assert from_anthropic_response(raw).text == "answer"


class StubAnthropic:
    def __init__(self, result: Any) -> None:
        self._result = result
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def anthropic_provider(result: Any) -> tuple[AnthropicProvider, StubAnthropic]:
    provider = AnthropicProvider(model="claude-opus-5", api_key="k")
    stub = StubAnthropic(result)
    provider._client = stub  # type: ignore[assignment]
    return provider, stub


def test_anthropic_request_shape() -> None:
    provider, stub = anthropic_provider(fake_anthropic_response())
    response = provider.complete(
        system="be helpful", messages=[Message.user("go")], tools=[SPEC], max_tokens=512
    )
    request = stub.requests[0]
    assert request["model"] == "claude-opus-5"
    assert request["max_tokens"] == 512
    assert request["system"] == "be helpful"
    assert request["tools"][0]["name"] == "read_file"
    assert response.provider == "anthropic"
    assert response.cost_usd is not None


def test_anthropic_omits_empty_system_and_tools() -> None:
    provider, stub = anthropic_provider(fake_anthropic_response())
    provider.complete(system="", messages=[Message.user("go")])
    assert "system" not in stub.requests[0]
    assert "tools" not in stub.requests[0]


def test_anthropic_rate_limit_maps_to_domain_error() -> None:
    error = anthropic.RateLimitError("slow down", response=http_response(429), body=None)
    provider, _ = anthropic_provider(error)
    with pytest.raises(LLMRateLimitError):
        provider.complete(system="", messages=[Message.user("go")])


# ------------------------------------------------------------------ openai ---


def test_openai_tool_uses_function_wrapper() -> None:
    rendered = to_openai_tool(SPEC)
    assert rendered["type"] == "function"
    assert rendered["function"]["name"] == "read_file"


def test_openai_assistant_tool_calls_are_json_encoded() -> None:
    rendered = to_openai_messages([Message.assistant("thinking", (CALL,))])
    assert json.loads(rendered[0]["tool_calls"][0]["function"]["arguments"]) == {"path": "a.py"}


def test_openai_tool_results_are_separate_messages() -> None:
    rendered = to_openai_messages([Message.tool_result(CALL, "one")])
    assert rendered[0]["role"] == "tool"
    assert rendered[0]["tool_call_id"] == "call_1"


def test_openai_conveys_tool_errors_in_content() -> None:
    """OpenAI has no error flag, so the failure has to be readable in the text."""
    rendered = to_openai_messages([Message.tool_result(CALL, "boom", is_error=True)])
    assert rendered[0]["content"].startswith("ERROR:")


def fake_openai_response(**overrides: Any) -> SimpleNamespace:
    message = SimpleNamespace(
        content="Hello",
        tool_calls=[
            SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(name="read_file", arguments='{"path": "a.py"}'),
            )
        ],
    )
    defaults: dict[str, Any] = {
        "choices": [SimpleNamespace(message=message, finish_reason="tool_calls")],
        "model": "gpt-4o",
        "usage": SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            prompt_tokens_details=SimpleNamespace(cached_tokens=10),
        ),
    }
    return SimpleNamespace(**{**defaults, **overrides})


def test_openai_response_normalisation() -> None:
    response = from_openai_response(fake_openai_response())
    assert response.text == "Hello"
    assert response.stop_reason is StopReason.TOOL_USE
    assert response.tool_calls[0].arguments == {"path": "a.py"}
    # Cached tokens are reported inside prompt_tokens and must not be counted twice.
    assert response.usage.input_tokens == 90
    assert response.usage.cache_read_tokens == 10


def test_openai_malformed_tool_arguments_do_not_crash() -> None:
    """A model can emit invalid JSON; the run must survive it."""
    raw = fake_openai_response()
    raw.choices[0].message.tool_calls[0].function.arguments = "{not json"
    assert from_openai_response(raw).tool_calls[0].arguments == {}


def test_openai_handles_missing_usage_details() -> None:
    raw = fake_openai_response(
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, prompt_tokens_details=None)
    )
    assert from_openai_response(raw).usage.input_tokens == 10


def test_openai_length_finish_reason() -> None:
    raw = fake_openai_response()
    raw.choices[0].finish_reason = "length"
    assert from_openai_response(raw).stop_reason is StopReason.MAX_TOKENS


class StubOpenAI:
    def __init__(self, result: Any) -> None:
        self._result = result
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def openai_provider(result: Any) -> tuple[OpenAIProvider, StubOpenAI]:
    provider = OpenAIProvider(model="gpt-4o", api_key="k")
    stub = StubOpenAI(result)
    provider._client = stub  # type: ignore[assignment]
    return provider, stub


def test_openai_request_shape() -> None:
    provider, stub = openai_provider(fake_openai_response())
    provider.complete(
        system="be helpful", messages=[Message.user("go")], tools=[SPEC], max_tokens=512
    )
    request = stub.requests[0]
    assert request["messages"][0] == {"role": "system", "content": "be helpful"}
    # `max_tokens` is rejected by reasoning models; the superseding name is used.
    assert request["max_completion_tokens"] == 512
    assert "max_tokens" not in request


def test_openai_rate_limit_maps_to_domain_error() -> None:
    error = openai.RateLimitError("slow down", response=http_response(429), body=None)
    provider, _ = openai_provider(error)
    with pytest.raises(LLMRateLimitError):
        provider.complete(system="", messages=[Message.user("go")])


def test_openai_status_error_maps_to_domain_error() -> None:
    error = openai.APIStatusError("bad", response=http_response(400), body=None)
    provider, _ = openai_provider(error)
    with pytest.raises(LLMError):
        provider.complete(system="", messages=[Message.user("go")])


def test_provider_repr_never_shows_a_credential() -> None:
    provider = OpenAIProvider(model="gpt-4o", api_key="sk-should-not-appear")
    assert "sk-should-not-appear" not in repr(provider)
