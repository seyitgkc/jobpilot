"""AI provider implementations behind one interface. Exactly three functions
touch a model; everything else in the system is deterministic code."""

from __future__ import annotations

from ..config import Config
from .base import AIProvider, NullProvider


def from_config(config: Config) -> AIProvider:
    if config.ai.provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(config.ai)
    return NullProvider()
