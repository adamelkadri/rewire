"""Model pricing, for turning token counts into money.

The table is a **dated snapshot**, not a live feed. Published prices change, and
a hard-coded number that silently goes stale is worse than an absent one — so an
unknown model returns ``None`` rather than ``0.0``. A zero would quietly
understate spend in exactly the reports Phase 9 exists to produce.

Every figure is USD per million tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from rewire.llm.models import Usage

#: When the prices below were recorded. Reported alongside every cost estimate
#: so a stale table is visible rather than assumed current.
PRICING_SNAPSHOT_DATE: Final[str] = "2026-06-24"


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """USD per million tokens for one model."""

    input: float
    output: float
    #: Cached-prefix reads, where the provider prices them separately.
    cache_read: float | None = None
    #: Writing a cache entry, typically a premium over ordinary input.
    cache_write: float | None = None

    def cost(self, usage: Usage) -> float:
        """Cost in USD for ``usage`` at these rates."""
        million = 1_000_000
        total = (usage.input_tokens * self.input + usage.output_tokens * self.output) / million
        if usage.cache_read_tokens:
            rate = self.cache_read if self.cache_read is not None else self.input
            total += usage.cache_read_tokens * rate / million
        if usage.cache_write_tokens:
            rate = self.cache_write if self.cache_write is not None else self.input
            total += usage.cache_write_tokens * rate / million
        return total


#: Anthropic list prices, per million tokens. Cache reads are ~0.1x input and
#: cache writes ~1.25x, per the published caching multipliers.
ANTHROPIC_PRICING: Final[dict[str, ModelPricing]] = {
    "claude-fable-5": ModelPricing(10.00, 50.00, 1.00, 12.50),
    "claude-opus-5": ModelPricing(5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": ModelPricing(5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-7": ModelPricing(5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-6": ModelPricing(5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5": ModelPricing(3.00, 15.00, 0.30, 3.75),
    "claude-sonnet-4-6": ModelPricing(3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5": ModelPricing(1.00, 5.00, 0.10, 1.25),
}

#: OpenAI list prices, per million tokens.
OPENAI_PRICING: Final[dict[str, ModelPricing]] = {
    "gpt-4o": ModelPricing(2.50, 10.00, 1.25),
    "gpt-4o-mini": ModelPricing(0.15, 0.60, 0.075),
    "gpt-4.1": ModelPricing(2.00, 8.00, 0.50),
    "gpt-4.1-mini": ModelPricing(0.40, 1.60, 0.10),
    "gpt-4.1-nano": ModelPricing(0.10, 0.40, 0.025),
    "o3": ModelPricing(2.00, 8.00, 0.50),
    "o3-mini": ModelPricing(1.10, 4.40, 0.55),
    "o4-mini": ModelPricing(1.10, 4.40, 0.275),
}

MODEL_PRICING: Final[dict[str, ModelPricing]] = {**ANTHROPIC_PRICING, **OPENAI_PRICING}


def pricing_for(model: str) -> ModelPricing | None:
    """Return the pricing for ``model``, or ``None`` if it is not in the table.

    Falls back to a longest-prefix match so that a dated snapshot identifier
    (``gpt-4o-2024-11-20``) resolves to its base model. The match must end on a
    separator, so ``gpt-4o-mini`` never resolves to ``gpt-4o``.
    """
    exact = MODEL_PRICING.get(model)
    if exact is not None:
        return exact

    candidates = [
        name
        for name in MODEL_PRICING
        if model.startswith(f"{name}-") or model.startswith(f"{name}@")
    ]
    if not candidates:
        return None
    return MODEL_PRICING[max(candidates, key=len)]


def estimate_cost(model: str, usage: Usage) -> float | None:
    """Estimate the USD cost of ``usage`` on ``model``.

    Returns ``None`` for a model absent from the table. Callers must render that
    as "unknown" rather than as free.
    """
    pricing = pricing_for(model)
    return None if pricing is None else pricing.cost(usage)


def known_models() -> list[str]:
    """Every model with a published price in this snapshot."""
    return sorted(MODEL_PRICING)


__all__ = [
    "ANTHROPIC_PRICING",
    "MODEL_PRICING",
    "OPENAI_PRICING",
    "PRICING_SNAPSHOT_DATE",
    "ModelPricing",
    "estimate_cost",
    "known_models",
    "pricing_for",
]
