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
        script: list[dict] | list[list[dict]] | None = None,
        per_token_delay_s: float = 0.005,
    ) -> None:
        # Default: echo the last user message in 5 chunks.
        self._script = script
        self._delay = per_token_delay_s
        self.calls = 0

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
        script = self._script_for_call(messages)
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
        finish = _finish_for_script(script)
        yield LLMDelta(
            finish=finish,
            usage=LLMUsage(tokens_in=_count(messages), tokens_out=_count_script(script)),
        )

    async def count_tokens(self, messages: list[ChatMessage]) -> int:
        return _count(messages)

    def _script_for_call(self, messages: list[ChatMessage]) -> list[dict]:
        self.calls += 1
        if self._script is None:
            return _default_echo_script(messages)
        if self._script and isinstance(self._script[0], list):
            scripts = self._script  # type: ignore[assignment]
            idx = min(self.calls - 1, len(scripts) - 1)
            return scripts[idx]  # type: ignore[index]
        return self._script  # type: ignore[return-value]


def _chunks(s: str, size: int):
    for i in range(0, len(s), size):
        yield s[i : i + size]


def _count(messages: list[ChatMessage]) -> int:
    return sum(max(1, len(m.content) // 3) for m in messages)


def _count_script(script: list[dict]) -> int:
    return sum(max(1, len(s.get("text", "")) // 3) for s in script if s.get("kind") == "text")


def _finish_for_script(script: list[dict]) -> LLMFinishReason:
    explicit = next((s.get("finish") for s in script if s.get("kind") == "finish"), None)
    if explicit:
        return LLMFinishReason(explicit)
    meaningful = [s for s in script if s.get("kind") != "finish"]
    if meaningful and meaningful[-1].get("kind") == "tool_call":
        return LLMFinishReason.TOOL_CALLS
    return LLMFinishReason.STOP


def _default_echo_script(messages: list[ChatMessage]) -> list[dict]:
    last_user = next((m for m in reversed(messages) if m.role.value == "user"), None)
    text = (last_user.content if last_user else "你好") + " — 我在这。"
    return [{"kind": "text", "text": text}]
