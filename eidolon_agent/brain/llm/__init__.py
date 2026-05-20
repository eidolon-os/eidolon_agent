"""Concrete :class:`LLMPort` adapters."""

from eidolon_agent.brain.llm.fake import FakeLLM
from eidolon_agent.brain.llm.litellm_provider import LiteLLMProvider

__all__ = ["FakeLLM", "LiteLLMProvider"]
