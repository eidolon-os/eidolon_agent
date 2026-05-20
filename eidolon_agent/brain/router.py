"""LLM router: model selection by name with a configured default.

The router itself implements :class:`LLMPort` so callers don't need to know
which concrete provider they're talking to.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from eidolon_agent.core.errors import LLMUnavailableError
from eidolon_agent.core.types.llm import LLMDelta
from eidolon_agent.core.types.messages import ChatMessage
from eidolon_agent.core.types.tool import ToolSchema

_log = logging.getLogger(__name__)


class LLMRouter:
    def __init__(self, *, providers: dict[str, object], default: str) -> None:
        if default not in providers:
            raise ValueError(f"default model {default!r} not in providers: {list(providers)}")
        self._providers = providers
        self._default = default

    @property
    def model_id(self) -> str:
        return self._default

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        request_id: str,
    ) -> AsyncIterator[LLMDelta]:
        key = model if model and model in self._providers else self._default
        provider = self._providers[key]
        try:
            async for delta in provider.stream(
                messages, tools=tools, temperature=temperature,
                max_tokens=max_tokens, request_id=request_id,
            ):
                yield delta
        except LLMUnavailableError:
            raise
        except Exception as exc:
            raise LLMUnavailableError(f"provider {key} failed: {exc}") from exc

    async def count_tokens(self, messages: list[ChatMessage]) -> int:
        return await self._providers[self._default].count_tokens(messages)
