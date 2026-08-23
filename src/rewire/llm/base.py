"""The provider interface every model adapter implements."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from rewire.core.errors import LLMError
from rewire.llm.models import LLMResponse, Message, ToolSpec
from rewire.llm.pricing import estimate_cost

#: Cap on output tokens when a caller does not specify one.
DEFAULT_MAX_OUTPUT_TOKENS = 8192


class LLMProvider(ABC):
    """A model Rewire can send a conversation to.

    Deliberately synchronous. An agent loop is inherently sequential — each
    request depends on the previous tool result — so async would add colour to
    the whole call chain and buy nothing. Phase 13 can add a concurrent wrapper
    if serving many migrations at once ever needs one.
    """

    #: Short identifier, e.g. ``anthropic``. Recorded in every trace.
    name: str

    def __init__(self, model: str, *, temperature: float = 0.0, timeout: float = 120.0) -> None:
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> LLMResponse:
        """Send a conversation and return the model's reply.

        Args:
            system: System prompt. Never contains repository content — see
                ``rewire.agents.prompts`` for why.
            messages: Conversation so far, oldest first.
            tools: Tools the model may invoke.
            max_tokens: Cap on generated tokens.

        Raises:
            LLMError: The request failed. Rate limits raise
                :class:`~rewire.core.errors.LLMRateLimitError`.
        """

    def _finalise(self, response: LLMResponse, *, latency_ms: float) -> LLMResponse:
        """Attach provider identity, latency and estimated cost to a response."""
        return response.model_copy(
            update={
                "provider": self.name,
                "model": response.model or self.model,
                "latency_ms": round(latency_ms, 2),
                "cost_usd": estimate_cost(response.model or self.model, response.usage),
            }
        )

    def __repr__(self) -> str:
        """Identify the provider and model, never any credential."""
        return f"{type(self).__name__}(model={self.model!r})"


@contextmanager
def measured() -> Iterator[list[float]]:
    """Time a block, appending the elapsed milliseconds to the yielded list."""
    started = time.perf_counter()
    elapsed: list[float] = []
    try:
        yield elapsed
    finally:
        elapsed.append((time.perf_counter() - started) * 1000)


def wrap_provider_error(provider: str, exc: Exception) -> LLMError:
    """Convert a provider SDK exception into a Rewire domain error.

    The message is taken from the exception, never from the request, so a
    credential passed in a header cannot be echoed into a log line.
    """
    return LLMError(f"{provider} request failed: {type(exc).__name__}: {exc}", provider=provider)


__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "LLMProvider",
    "measured",
    "wrap_provider_error",
]
