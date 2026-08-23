"""Tests for the provider-neutral LLM types and pricing."""

from __future__ import annotations

import pytest

from rewire.llm.models import LLMResponse, Message, Role, ToolCall, Usage
from rewire.llm.pricing import (
    MODEL_PRICING,
    ModelPricing,
    estimate_cost,
    known_models,
    pricing_for,
)


def test_message_constructors_set_roles() -> None:
    assert Message.user("hi").role is Role.USER
    assert Message.assistant("ok").role is Role.ASSISTANT


def test_tool_result_quotes_the_call() -> None:
    call = ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})
    result = Message.tool_result(call, "contents", is_error=True)
    assert result.role is Role.TOOL
    assert result.tool_call_id == "call_1"
    assert result.tool_name == "read_file"
    assert result.is_error


def test_usage_totals_and_addition() -> None:
    first = Usage(input_tokens=10, output_tokens=5, cache_read_tokens=2)
    second = Usage(input_tokens=1, output_tokens=1, cache_write_tokens=3)
    combined = first + second
    assert combined.input_tokens == 11
    assert combined.cache_read_tokens == 2
    assert combined.cache_write_tokens == 3
    assert combined.output_tokens == 6
    assert combined.total_tokens == 22


def test_response_reports_whether_tools_were_requested() -> None:
    plain = LLMResponse(text="done")
    assert not plain.wants_tools
    calling = LLMResponse(tool_calls=(ToolCall(id="1", name="t"),))
    assert calling.wants_tools


def test_response_converts_to_a_continuing_message() -> None:
    call = ToolCall(id="1", name="t")
    message = LLMResponse(text="thinking", tool_calls=(call,)).to_message()
    assert message.role is Role.ASSISTANT
    assert message.tool_calls == (call,)


# ------------------------------------------------------------------ pricing --


def test_known_models_are_priced() -> None:
    assert "claude-opus-5" in known_models()
    assert "gpt-4o" in known_models()


def test_cost_is_computed_per_million_tokens() -> None:
    pricing = ModelPricing(input=10.0, output=100.0)
    cost = pricing.cost(Usage(input_tokens=1_000_000, output_tokens=1_000_000))
    assert cost == pytest.approx(110.0)


def test_cache_reads_are_priced_separately() -> None:
    pricing = ModelPricing(input=10.0, output=10.0, cache_read=1.0)
    cached = pricing.cost(Usage(cache_read_tokens=1_000_000))
    assert cached == pytest.approx(1.0)


def test_cache_tokens_fall_back_to_input_rate() -> None:
    pricing = ModelPricing(input=10.0, output=10.0)
    assert pricing.cost(Usage(cache_read_tokens=1_000_000)) == pytest.approx(10.0)


def test_dated_snapshots_resolve_to_their_base_model() -> None:
    assert pricing_for("gpt-4o-2024-11-20") is MODEL_PRICING["gpt-4o"]


def test_prefix_matching_does_not_confuse_variants() -> None:
    """`gpt-4o-mini` must never be priced as `gpt-4o`."""
    assert pricing_for("gpt-4o-mini") is MODEL_PRICING["gpt-4o-mini"]
    assert pricing_for("gpt-4o-mini") is not MODEL_PRICING["gpt-4o"]


def test_unknown_models_cost_none_not_zero() -> None:
    """A zero would silently understate spend in every cost report."""
    assert pricing_for("some-unreleased-model") is None
    assert estimate_cost("some-unreleased-model", Usage(input_tokens=1_000_000)) is None
