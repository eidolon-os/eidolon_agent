"""LiteLLMProvider — chunk parsing & error wrapping. SDK is monkey-patched.

We stub :func:`litellm.acompletion` to return a synthetic async stream so
the provider's chunk-decoding loop runs without any network. The test
exercises content streaming, tool-call buffering across chunks, usage
delta emission, and error wrapping.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone

import litellm
import pytest

from eidolon_agent.core.errors import LLMUnavailableError
from eidolon_agent.core.types.llm import LLMFinishReason
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.infra.llm.providers.litellm_provider import LiteLLMProvider

pytestmark = pytest.mark.unit


# ---- synthetic chunk types matching the litellm.ModelResponseStream API ----


@dataclass
class _ToolCallFunc:
    name: str | None = None
    arguments: str | None = None


@dataclass
class _ToolCallChunk:
    index: int
    id: str | None = None
    function: _ToolCallFunc | None = None


@dataclass
class _Delta:
    content: str | None = None
    tool_calls: list[_ToolCallChunk] | None = None


@dataclass
class _Choice:
    delta: _Delta
    finish_reason: str | None = None


@dataclass
class _Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class _Chunk:
    choices: list[_Choice]
    usage: _Usage | None = None


async def _aiter(items: list) -> AsyncIterator:
    for it in items:
        yield it


def _msg(content: str) -> ChatMessage:
    return ChatMessage(
        id=uuid.uuid4().hex,
        role=MessageRole.USER,
        content=content,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def provider() -> LiteLLMProvider:
    return LiteLLMProvider(model="openai/test", api_key="k", api_base="http://x/v1")


async def test_text_chunks_yield_text_delta(monkeypatch: pytest.MonkeyPatch, provider) -> None:
    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(content="hello "))]),
        _Chunk(choices=[_Choice(delta=_Delta(content="world"))]),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="stop")]),
    ]

    async def _fake(**kwargs):
        return _aiter(chunks)

    monkeypatch.setattr(litellm, "acompletion", _fake)
    out = [d async for d in provider.stream([_msg("hi")], request_id="r")]
    text = "".join(d.text_delta or "" for d in out)
    assert text == "hello world"
    assert any(d.finish is LLMFinishReason.STOP for d in out)


async def test_tool_call_buffered_across_chunks(monkeypatch: pytest.MonkeyPatch, provider) -> None:
    # Tool-call deltas arrive piecewise: id+name first, then arguments
    # split across two chunks.
    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(tool_calls=[
            _ToolCallChunk(index=0, id="call-1", function=_ToolCallFunc(name="get_time"))
        ]))]),
        _Chunk(choices=[_Choice(delta=_Delta(tool_calls=[
            _ToolCallChunk(index=0, function=_ToolCallFunc(arguments='{"tz":"'))
        ]))]),
        _Chunk(choices=[_Choice(delta=_Delta(tool_calls=[
            _ToolCallChunk(index=0, function=_ToolCallFunc(arguments='UTC"}'))
        ]))]),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="tool_calls")]),
    ]

    async def _fake(**kwargs):
        return _aiter(chunks)

    monkeypatch.setattr(litellm, "acompletion", _fake)
    out = [d async for d in provider.stream([_msg("when")], request_id="r")]
    tcs = [d.tool_call for d in out if d.tool_call]
    assert len(tcs) == 1
    assert tcs[0].id == "call-1"
    assert tcs[0].name == "get_time"
    assert tcs[0].arguments == {"tz": "UTC"}
    finishes = [d.finish for d in out if d.finish is not None]
    assert finishes == [LLMFinishReason.TOOL_CALLS]


async def test_malformed_tool_args_falls_back_to_raw(monkeypatch: pytest.MonkeyPatch, provider) -> None:
    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(tool_calls=[
            _ToolCallChunk(index=0, id="c", function=_ToolCallFunc(name="x", arguments="{not-json"))
        ]))]),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="tool_calls")]),
    ]

    async def _fake(**kwargs):
        return _aiter(chunks)

    monkeypatch.setattr(litellm, "acompletion", _fake)
    out = [d async for d in provider.stream([_msg("x")], request_id="r")]
    [tc] = [d.tool_call for d in out if d.tool_call]
    assert tc.arguments == {"_raw": "{not-json"}


async def test_usage_chunk_emits_usage_delta(monkeypatch: pytest.MonkeyPatch, provider) -> None:
    chunks = [
        _Chunk(
            choices=[_Choice(delta=_Delta(content="x"))],
            usage=_Usage(prompt_tokens=10, completion_tokens=3),
        ),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="stop")]),
    ]

    async def _fake(**kwargs):
        return _aiter(chunks)

    monkeypatch.setattr(litellm, "acompletion", _fake)
    out = [d async for d in provider.stream([_msg("x")], request_id="r")]
    usages = [d.usage for d in out if d.usage]
    assert len(usages) == 1
    assert usages[0].tokens_in == 10
    assert usages[0].tokens_out == 3


async def test_acompletion_exception_wrapped_as_llm_unavailable(
    monkeypatch: pytest.MonkeyPatch, provider
) -> None:
    async def _fake(**kwargs):
        raise RuntimeError("DNS fail")

    monkeypatch.setattr(litellm, "acompletion", _fake)
    with pytest.raises(LLMUnavailableError, match="litellm call failed: DNS fail"):
        async for _ in provider.stream([_msg("x")], request_id="r"):
            pass


async def test_mid_stream_exception_wrapped(monkeypatch: pytest.MonkeyPatch, provider) -> None:
    async def _broken_iter():
        yield _Chunk(choices=[_Choice(delta=_Delta(content="a"))])
        raise RuntimeError("connection reset")

    async def _fake(**kwargs):
        return _broken_iter()

    monkeypatch.setattr(litellm, "acompletion", _fake)
    with pytest.raises(LLMUnavailableError, match="litellm stream error"):
        async for _ in provider.stream([_msg("x")], request_id="r"):
            pass


async def test_count_tokens_falls_back_when_litellm_raises(
    monkeypatch: pytest.MonkeyPatch, provider
) -> None:
    def _boom(**kwargs):
        raise RuntimeError("no model in cost map")

    monkeypatch.setattr(litellm, "token_counter", _boom)
    # 30-char content → 30 // 3 == 10 tokens approx
    n = await provider.count_tokens([_msg("a" * 30)])
    assert n >= 1
