"""Constructing a provider from application settings."""

from __future__ import annotations

from rewire.core.config import LLMSettings
from rewire.core.errors import ConfigurationError
from rewire.llm.base import LLMProvider


def build_provider(settings: LLMSettings) -> LLMProvider:
    """Build the configured provider.

    Adapters are imported lazily so that the deterministic phases never pay for
    a provider SDK import, and so a broken install of one SDK cannot stop the
    other from working.

    Raises:
        ConfigurationError: No provider is configured, or the selected one has
            no credential.
    """
    provider = settings.provider
    if provider == "null":
        raise ConfigurationError(
            "no LLM provider is configured",
            remedy="set REWIRE_LLM__PROVIDER and the matching API key",
        )

    key = getattr(settings, f"{provider}_api_key", None)
    if key is None or not key.get_secret_value():
        raise ConfigurationError(
            f"provider {provider!r} is selected but has no API key",
            remedy=f"set REWIRE_LLM__{provider.upper()}_API_KEY",
        )

    if provider == "anthropic":
        from rewire.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            model=settings.model,
            api_key=key,
            temperature=settings.temperature,
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
        )

    from rewire.llm.openai_provider import OpenAIProvider

    # OpenRouter speaks the Chat Completions dialect, so the OpenAI adapter
    # serves it with a different base URL.
    base_url = "https://openrouter.ai/api/v1" if provider == "openrouter" else None
    return OpenAIProvider(
        model=settings.model,
        api_key=key,
        temperature=settings.temperature,
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
        base_url=base_url,
    )


__all__ = ["build_provider"]
