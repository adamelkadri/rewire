"""A deterministic provider that replays canned responses.

Exists so the agent loop can be tested exhaustively without a network, an API
key, or a bill. This is not a stub standing in for unfinished work — the loop's
branching (tool errors, budget exhaustion, malformed edits, refusal to stop) is
far easier to drive from a script than from a real model, and a test suite that
depended on a live model would be neither deterministic nor runnable in CI.

It also records every request it receives, which is what lets tests assert on
what the agent actually sent — including that repository content never reaches
the system prompt.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from rewire.core.errors import LLMError
from rewire.llm.base import DEFAULT_MAX_OUTPUT_TOKENS, LLMProvider
from rewire.llm.models import LLMResponse, Message, StopReason, ToolCall, ToolSpec, Usage


@dataclass(slots=True)
class RecordedRequest:
    """One call the agent made, captured for assertions."""

    system: str
    messages: tuple[Message, ...]
    tools: tuple[str, ...]
    max_tokens: int


class ScriptedProvider(LLMProvider):
    """Returns a fixed sequence of responses, in order."""

    name = "scripted"

    def __init__(
        self,
        responses: Sequence[LLMResponse],
        *,
        model: str = "scripted-model",
        repeat_last: bool = False,
    ) -> None:
        """Build a provider that replays ``responses``.

        Args:
            responses: Replies to return, in order.
            model: Name reported on each response.
            repeat_last: When the script runs out, repeat the final response
                instead of raising. Useful for testing an agent that refuses to
                terminate, where the iteration cap is what should stop it.
        """
        super().__init__(model)
        self._responses = list(responses)
        self._repeat_last = repeat_last
        self.requests: list[RecordedRequest] = []
        self.call_count = 0

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> LLMResponse:
        """Return the next scripted response."""
        self.requests.append(
            RecordedRequest(
                system=system,
                messages=tuple(messages),
                tools=tuple(tool.name for tool in tools),
                max_tokens=max_tokens,
            )
        )
        index = self.call_count
        self.call_count += 1

        if index < len(self._responses):
            return self._finalise(self._responses[index], latency_ms=0.0)
        if self._repeat_last and self._responses:
            return self._finalise(self._responses[-1], latency_ms=0.0)
        raise LLMError(
            "scripted provider ran out of responses",
            calls=self.call_count,
            scripted=len(self._responses),
        )

    @property
    def last_request(self) -> RecordedRequest | None:
        """The most recent request, if any."""
        return self.requests[-1] if self.requests else None


@dataclass(slots=True)
class ScriptBuilder:
    """Small helper for assembling scripted responses in tests."""

    responses: list[LLMResponse] = field(default_factory=list)
    _next_id: int = 0

    def says(self, text: str, *, stop: StopReason = StopReason.END_TURN) -> ScriptBuilder:
        """Append a plain text reply."""
        self.responses.append(
            LLMResponse(
                text=text,
                stop_reason=stop,
                usage=Usage(input_tokens=100, output_tokens=20),
            )
        )
        return self

    def calls(self, name: str, **arguments: object) -> ScriptBuilder:
        """Append a reply that invokes one tool."""
        self._next_id += 1
        self.responses.append(
            LLMResponse(
                tool_calls=(
                    ToolCall(id=f"call_{self._next_id}", name=name, arguments=dict(arguments)),
                ),
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(input_tokens=100, output_tokens=20),
            )
        )
        return self

    def build(self, *, repeat_last: bool = False) -> ScriptedProvider:
        """Return a provider replaying the accumulated script."""
        return ScriptedProvider(self.responses, repeat_last=repeat_last)


__all__ = ["RecordedRequest", "ScriptBuilder", "ScriptedProvider"]
