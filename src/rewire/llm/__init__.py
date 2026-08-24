"""Provider-agnostic LLM abstraction.

No provider SDK is imported outside this package. Phase 9 compares models by
swapping the object returned from :func:`build_provider`; that comparison is
only credible if nothing else changes with it.
"""

from rewire.llm.base import DEFAULT_MAX_OUTPUT_TOKENS, LLMProvider
from rewire.llm.models import (
    LLMResponse,
    Message,
    Role,
    StopReason,
    ToolCall,
    ToolSpec,
    Usage,
)
from rewire.llm.pricing import PRICING_SNAPSHOT_DATE, estimate_cost, known_models, pricing_for
from rewire.llm.registry import (
    SUPPORTED_PROVIDERS,
    build_provider,
    build_provider_for,
    credential_for,
)
from rewire.llm.scripted import ScriptBuilder, ScriptedProvider

__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "PRICING_SNAPSHOT_DATE",
    "SUPPORTED_PROVIDERS",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "Role",
    "ScriptBuilder",
    "ScriptedProvider",
    "StopReason",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "build_provider",
    "build_provider_for",
    "credential_for",
    "estimate_cost",
    "known_models",
    "pricing_for",
]
