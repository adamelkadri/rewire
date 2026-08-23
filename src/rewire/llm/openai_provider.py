"""OpenAI Chat Completions adapter.

Translates Rewire's neutral conversation model into OpenAI's message array and
back. Nothing outside this module imports the ``openai`` package.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import openai
from pydantic import SecretStr

from rewire.core.errors import LLMError, LLMRateLimitError
from rewire.llm.base import DEFAULT_MAX_OUTPUT_TOKENS, LLMProvider, measured, wrap_provider_error
from rewire.llm.models import (
    LLMResponse,
    Message,
    Role,
    StopReason,
    ToolCall,
    ToolSpec,
    Usage,
)

#: OpenAI finish reasons mapped onto Rewire's neutral set.
STOP_REASONS: dict[str, StopReason] = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
}


class OpenAIProvider(LLMProvider):
    """Calls OpenAI models through the Chat Completions API."""

    name = "openai"

    def __init__(
        self,
        model: str = "gpt-4o",
        *,
        api_key: SecretStr | str | None = None,
        temperature: float = 0.0,
        timeout: float = 120.0,
        max_retries: int = 3,
        base_url: str | None = None,
    ) -> None:
        super().__init__(model, temperature=temperature, timeout=timeout)
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        self._client = openai.OpenAI(
            api_key=key, timeout=timeout, max_retries=max_retries, base_url=base_url
        )

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> LLMResponse:
        """Send a conversation to OpenAI and normalise the reply."""
        payload = to_openai_messages(messages)
        if system:
            payload.insert(0, {"role": "system", "content": system})

        request: dict[str, Any] = {
            "model": self.model,
            "messages": payload,
            # `max_completion_tokens` supersedes `max_tokens`, which reasoning
            # models reject outright.
            "max_completion_tokens": max_tokens,
        }
        if tools:
            request["tools"] = [to_openai_tool(tool) for tool in tools]

        try:
            with measured() as elapsed:
                raw = self._client.chat.completions.create(**request)
        except openai.RateLimitError as exc:
            raise LLMRateLimitError(
                "openai rate limit exceeded", provider=self.name, model=self.model
            ) from exc
        except openai.APIStatusError as exc:
            raise LLMError(
                f"openai returned {exc.status_code}",
                provider=self.name,
                model=self.model,
                status_code=exc.status_code,
            ) from exc
        except openai.OpenAIError as exc:
            raise wrap_provider_error(self.name, exc) from exc

        return self._finalise(from_openai_response(raw), latency_ms=elapsed[0])


def to_openai_tool(tool: ToolSpec) -> dict[str, Any]:
    """Render a tool specification in OpenAI's function schema."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters or {"type": "object", "properties": {}},
        },
    }


def to_openai_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Convert neutral messages into OpenAI's message array."""
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role is Role.TOOL:
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id or "",
                    # OpenAI has no error flag on a tool result, so failure is
                    # conveyed in the content the model reads.
                    "content": f"ERROR: {message.content}" if message.is_error else message.content,
                }
            )
            continue

        if message.role is Role.ASSISTANT:
            entry: dict[str, Any] = {"role": "assistant", "content": message.content or None}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in message.tool_calls
                ]
            converted.append(entry)
            continue

        converted.append({"role": message.role.value, "content": message.content})
    return converted


def from_openai_response(raw: Any) -> LLMResponse:
    """Normalise an OpenAI response into an :class:`LLMResponse`."""
    choice = raw.choices[0]
    message = choice.message

    calls: list[ToolCall] = []
    for call in message.tool_calls or ():
        # Arguments arrive as a JSON string. A model can emit malformed JSON, so
        # the failure is carried into the tool call as empty arguments and
        # reported to the model as a tool error rather than crashing the run.
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except ValueError:
            arguments = {}
        calls.append(
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )

    usage = raw.usage
    cached = 0
    if usage is not None and getattr(usage, "prompt_tokens_details", None) is not None:
        cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0

    return LLMResponse(
        text=(message.content or "").strip(),
        tool_calls=tuple(calls),
        stop_reason=STOP_REASONS.get(choice.finish_reason or "", StopReason.OTHER),
        usage=Usage(
            input_tokens=max((getattr(usage, "prompt_tokens", 0) or 0) - cached, 0),
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cache_read_tokens=cached,
        ),
        model=getattr(raw, "model", ""),
    )


__all__ = [
    "STOP_REASONS",
    "OpenAIProvider",
    "from_openai_response",
    "to_openai_messages",
    "to_openai_tool",
]
