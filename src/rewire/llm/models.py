"""Provider-neutral types for talking to a language model.

Every provider speaks a different dialect: Anthropic returns a list of content
blocks, OpenAI returns a message with a parallel ``tool_calls`` array. This
module defines the shape Rewire uses internally, and each adapter translates in
both directions.

The translation layer exists for one concrete reason: Phase 9 runs identical
evaluation tasks across models and publishes a comparison table. That result is
only credible if swapping providers changes one injected object and nothing
else, so no provider SDK type is allowed to escape ``rewire.llm``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Role(StrEnum):
    """Who produced a message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class StopReason(StrEnum):
    """Why the model stopped generating."""

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    OTHER = "other"


class ToolCall(BaseModel):
    """A model's request to invoke one tool."""

    model_config = ConfigDict(frozen=True)

    #: Provider-assigned identifier. The matching result must quote it back.
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """One turn in a conversation.

    A single type covers all four roles rather than a class hierarchy, because
    every provider ultimately serialises them into one array and the variants
    differ by which fields are populated, not by behaviour.
    """

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str = ""
    #: Populated on assistant turns that request tool invocations.
    tool_calls: tuple[ToolCall, ...] = ()
    #: Set on tool results, quoting the call being answered.
    tool_call_id: str | None = None
    tool_name: str | None = None
    #: Whether a tool result represents a failure. Failures are returned to the
    #: model rather than raised, so it can correct itself.
    is_error: bool = False

    @classmethod
    def user(cls, content: str) -> Message:
        """Build a user message."""
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: tuple[ToolCall, ...] = ()) -> Message:
        """Build an assistant message."""
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool_result(cls, call: ToolCall, content: str, *, is_error: bool = False) -> Message:
        """Build the result of a tool invocation."""
        return cls(
            role=Role.TOOL,
            content=content,
            tool_call_id=call.id,
            tool_name=call.name,
            is_error=is_error,
        )


class ToolSpec(BaseModel):
    """A tool offered to the model, described by JSON Schema."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    #: JSON Schema for the tool's arguments. Kept as a plain mapping for the
    #: same reason API schemas are (see ADR-012): the dialect is large and a
    #: partial model would silently drop keywords.
    parameters: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    """Token accounting for one request."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    #: Cached-prefix tokens, where the provider reports them. Priced differently
    #: from ordinary input tokens.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Every token billed for this request."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def __add__(self, other: Usage) -> Usage:
        """Combine usage across requests, for per-run totals."""
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


class LLMResponse(BaseModel):
    """One model response, normalised across providers."""

    model_config = ConfigDict(frozen=True)

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: StopReason = StopReason.END_TURN
    usage: Usage = Field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    latency_ms: float = 0.0
    #: Estimated cost in USD, or ``None`` when the model is not in the pricing
    #: table. Never defaulted to zero: a silent 0.0 would understate spend.
    cost_usd: float | None = None

    @property
    def wants_tools(self) -> bool:
        """Whether the model asked to invoke tools."""
        return bool(self.tool_calls)

    def to_message(self) -> Message:
        """Convert to the assistant message that continues the conversation."""
        return Message.assistant(content=self.text, tool_calls=self.tool_calls)


__all__ = [
    "LLMResponse",
    "Message",
    "Role",
    "StopReason",
    "ToolCall",
    "ToolSpec",
    "Usage",
]
