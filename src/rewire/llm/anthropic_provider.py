"""Anthropic Messages API adapter.

Translates Rewire's neutral conversation model into Anthropic content blocks and
back. Nothing outside this module imports the ``anthropic`` package.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import anthropic
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

#: Anthropic stop reasons mapped onto Rewire's neutral set.
STOP_REASONS: dict[str, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.END_TURN,
    "refusal": StopReason.REFUSAL,
    "pause_turn": StopReason.OTHER,
}


class AnthropicProvider(LLMProvider):
    """Calls Claude through the Messages API."""

    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-opus-5",
        *,
        api_key: SecretStr | str | None = None,
        temperature: float = 0.0,
        timeout: float = 120.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__(model, temperature=temperature, timeout=timeout)
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        # A None key lets the SDK resolve credentials from the environment,
        # which is the documented behaviour and avoids Rewire ever holding one
        # it was not given.
        self._client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=max_retries)

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> LLMResponse:
        """Send a conversation to Claude and normalise the reply."""
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": to_anthropic_messages(messages),
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [to_anthropic_tool(tool) for tool in tools]

        try:
            with measured() as elapsed:
                raw = self._client.messages.create(**request)
        except anthropic.RateLimitError as exc:
            raise LLMRateLimitError(
                "anthropic rate limit exceeded", provider=self.name, model=self.model
            ) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(
                f"anthropic returned {exc.status_code}",
                provider=self.name,
                model=self.model,
                status_code=exc.status_code,
            ) from exc
        except anthropic.APIError as exc:
            raise wrap_provider_error(self.name, exc) from exc

        return self._finalise(from_anthropic_response(raw), latency_ms=elapsed[0])


def to_anthropic_tool(tool: ToolSpec) -> dict[str, Any]:
    """Render a tool specification in Anthropic's schema."""
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters or {"type": "object", "properties": {}},
    }


def to_anthropic_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Convert neutral messages into Anthropic's content-block form.

    Consecutive tool results are merged into one user turn. Anthropic expects
    every result for a parallel batch of calls in a single message, and
    splitting them trains the model to stop making parallel calls.
    """
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role is Role.TOOL:
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id or "",
                "content": message.content,
            }
            if message.is_error:
                block["is_error"] = True
            if converted and converted[-1]["role"] == "user" and _is_tool_result(converted[-1]):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            continue

        if message.role is Role.ASSISTANT:
            content: list[dict[str, Any]] = []
            if message.content:
                content.append({"type": "text", "text": message.content})
            content.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
                for call in message.tool_calls
            )
            converted.append({"role": "assistant", "content": content})
            continue

        converted.append({"role": "user", "content": message.content})
    return converted


def _is_tool_result(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and all(block.get("type") == "tool_result" for block in content)
    )


def from_anthropic_response(raw: Any) -> LLMResponse:
    """Normalise an Anthropic response into an :class:`LLMResponse`."""
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in raw.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            # Inputs are already parsed objects from the SDK; never string-match
            # on the serialised form, whose escaping varies by model.
            calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))

    usage = raw.usage
    return LLMResponse(
        text="\n".join(text_parts).strip(),
        tool_calls=tuple(calls),
        stop_reason=STOP_REASONS.get(raw.stop_reason or "", StopReason.OTHER),
        usage=Usage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        ),
        model=getattr(raw, "model", ""),
    )


__all__ = [
    "STOP_REASONS",
    "AnthropicProvider",
    "from_anthropic_response",
    "to_anthropic_messages",
    "to_anthropic_tool",
]
