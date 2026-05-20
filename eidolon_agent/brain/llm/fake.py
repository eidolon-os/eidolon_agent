"""Deterministic in-memory LLM used by tests and quick local demos.

Mimics token streaming by chunking a script. Supports a single tool-call
script entry to exercise the tool loop without a real model.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason, LLMUsage
from eidolon_agent.core.types.messages import ChatMessage
from eidolon_agent.core.types.tool import ToolCall, ToolSchema


class FakeLLM:
    """Yields a scripted sequence of deltas. See ``script`` for shape."""

    model_id = "fake:scripted"

    def __init__(
        self,
        *,
        script: list[dict] | None = None,
        per_token_delay_s: float = 0.005,
    ) -> None:
        # Default: echo the last user message in 5 chunks.
        self._script = script
        self._delay = per_token_delay_s

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
        script = self._script or _default_echo_script(messages)
        for step in script:
            if step["kind"] == "text":
                text = step["text"]
                # chunk to simulate token streaming
                for piece in _chunks(text, 12):
                    yield LLMDelta(text_delta=piece)
                    await asyncio.sleep(self._delay)
            elif step["kind"] == "tool_call":
                yield LLMDelta(
                    tool_call=ToolCall(
                        id=step.get("call_id") or uuid.uuid4().hex,
                        name=step["name"],
                        arguments=step.get("arguments") or {},
                    )
                )
        yield LLMDelta(
            finish=LLMFinishReason.STOP,
            usage=LLMUsage(tokens_in=_count(messages), tokens_out=_count_script(script)),
        )

    async def count_tokens(self, messages: list[ChatMessage]) -> int:
        return _count(messages)


def _chunks(s: str, size: int):
    for i in range(0, len(s), size):
        yield s[i : i + size]


def _count(messages: list[ChatMessage]) -> int:
    return sum(max(1, len(m.content) // 3) for m in messages)


def _count_script(script: list[dict]) -> int:
    return sum(max(1, len(s.get("text", "")) // 3) for s in script if s.get("kind") == "text")


def _default_echo_script(messages: list[ChatMessage]) -> list[dict]:
    last_user = next((m for m in reversed(messages) if m.role.value == "user"), None)
    text = (last_user.content if last_user else "你好") + " — 我在这。"
    return [{"kind": "text", "text": text}]
