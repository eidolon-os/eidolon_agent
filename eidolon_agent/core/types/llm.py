"""LLM streaming types. Provider-agnostic.

Each :class:`LLMPort` implementation maps its native stream events into these.
The Turn pipeline consumes only this shape; nothing downstream knows whether
the provider is OpenAI, Anthropic, vLLM, etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from eidolon_agent.core.types.tool import ToolCall


class LLMFinishReason(str, Enum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class LLMUsage:
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens_in: int = 0  # prompt-cache hits, if reported
    cost_usd_micro: int = 0


@dataclass(frozen=True, slots=True)
class LLMDelta:
    """A single increment from a streaming LLM call.

    Exactly one of {text_delta, tool_call, finish, usage} is non-None per
    instance. Discriminating by None lets the consumer use a single async loop.
    """

    text_delta: str | None = None
    tool_call: ToolCall | None = None
    finish: LLMFinishReason | None = None
    usage: LLMUsage | None = None
    raw: dict = field(default_factory=dict)
