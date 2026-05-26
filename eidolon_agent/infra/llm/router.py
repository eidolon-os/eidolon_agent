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
    def __init__(
        self,
        *,
        providers: dict[str, object],
        default: str,
        fallback_models: list[str] | None = None,
    ) -> None:
        if default not in providers:
            raise ValueError(f"default model {default!r} not in providers: {list(providers)}")
        self._providers = providers
        self._default = default
        self._fallback_models = [
            name for name in (fallback_models or []) if name in providers and name != default
        ]

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
        candidates = [key, *(m for m in self._fallback_models if m != key)]
        last_error: LLMUnavailableError | None = None
        for candidate in candidates:
            provider = self._providers[candidate]
            emitted = False
            try:
                async for delta in provider.stream(
                    messages, tools=tools, temperature=temperature,
                    max_tokens=max_tokens, request_id=request_id,
                ):
                    emitted = True
                    yield delta
                return
            except LLMUnavailableError as exc:
                if emitted:
                    raise
                last_error = exc
                _log.warning(
                    "llm provider %s failed before first chunk; trying fallback",
                    candidate,
                )
            except Exception as exc:
                if emitted:
                    raise LLMUnavailableError(f"provider {candidate} failed: {exc}") from exc
                last_error = LLMUnavailableError(f"provider {candidate} failed: {exc}")
                _log.warning(
                    "llm provider %s failed before first chunk; trying fallback",
                    candidate,
                )
        if last_error is not None:
            raise last_error
        raise LLMUnavailableError("no LLM providers configured")

    async def count_tokens(self, messages: list[ChatMessage]) -> int:
        return await self._providers[self._default].count_tokens(messages)

    async def warmup_default(self, *, timeout_s: float) -> bool:
        provider = self._providers[self._default]
        warmup = getattr(provider, "warmup", None)
        if warmup is None:
            return False
        return bool(await warmup(timeout_s=timeout_s))

    async def close(self) -> None:
        seen: set[int] = set()
        for provider in self._providers.values():
            close = getattr(provider, "close", None)
            if close is None or id(provider) in seen:
                continue
            seen.add(id(provider))
            await close()
