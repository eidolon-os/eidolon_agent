"""Concrete :class:`LLMPort` adapters."""

from eidolon_agent.infra.llm.providers.fake import FakeLLM
from eidolon_agent.infra.llm.providers.litellm_provider import LiteLLMProvider

__all__ = ["FakeLLM", "LiteLLMProvider"]
