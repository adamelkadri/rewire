"""Constructing a provider from application settings."""

from __future__ import annotations

from typing import Final

from rewire.core.config import LLMSettings
from rewire.core.errors import ConfigurationError
from rewire.llm.base import LLMProvider

#: Providers Rewire has an adapter for. ``null`` is the unconfigured default and
#: is deliberately absent: it is a state, not a destination.
SUPPORTED_PROVIDERS: Final[frozenset[str]] = frozenset({"anthropic", "openai", "openrouter"})


def credential_for(settings: LLMSettings, provider: str) -> str | None:
    """The configured API key for ``provider``, or ``None`` if there is not one.

    Lets a caller find out whether a model is runnable *before* spending an hour
    of benchmark time discovering it is not.
    """
    secret = getattr(settings, f"{provider}_api_key", None)
    if secret is None:
        return None
    key = secret.get_secret_value()
    return key or None


def build_provider_for(settings: LLMSettings, *, provider: str, model: str) -> LLMProvider:
    """Build a provider for an explicit provider/model pair.

    Everything else — temperature, timeout, retry budget — comes from the same
    settings, so a comparison between two models differs in the model and
    nothing else.

    Raises:
        ConfigurationError: The provider is unknown or has no credential.
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigurationError(
            f"unknown provider {provider!r}",
            supported=sorted(SUPPORTED_PROVIDERS),
        )
    return build_provider(settings.model_copy(update={"provider": provider, "model": model}))


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


__all__ = [
    "SUPPORTED_PROVIDERS",
    "build_provider",
    "build_provider_for",
    "credential_for",
]
