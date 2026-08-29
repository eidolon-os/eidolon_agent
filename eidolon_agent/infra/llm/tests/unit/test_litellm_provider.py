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
from eidolon_agent.core.types.llm import LLMActivityKind, LLMFinishReason
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.infra.llm.providers.litellm_provider import LiteLLMProvider, _to_msg

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
    reasoning_content: str | None = None
    provider_specific_fields: dict | None = None
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


def test_assistant_tool_calls_render_as_openai_tool_call_messages() -> None:
    msg = ChatMessage(
        id=uuid.uuid4().hex,
        role=MessageRole.ASSISTANT,
        content="",
        created_at=datetime.now(timezone.utc),
        tool_calls=(
            ToolCall(
                id="call-1",
                name="delegate_to_coworker",
                arguments={"instruction": "整理资料"},
            ),
        ),
    )

    out = _to_msg(msg)

    assert out == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "delegate_to_coworker",
                    "arguments": '{"instruction": "整理资料"}',
                },
            }
        ],
    }


@pytest.fixture
def provider() -> LiteLLMProvider:
    return LiteLLMProvider(
        model="openai/test",
        api_key="k",
        api_base="http://x/v1",
        shared_http_client=False,
    )


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


async def test_disabled_thinking_is_sent_as_provider_extra_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _aiter([_Chunk(choices=[_Choice(delta=_Delta(content="869"))])])

    monkeypatch.setattr(litellm, "acompletion", _fake)
    fast_provider = LiteLLMProvider(
        model="openai/deepseek-v4-flash",
        api_key="k",
        api_base="https://api.deepseek.com/v1",
        shared_http_client=False,
        thinking="disabled",
    )

    out = [d async for d in fast_provider.stream([_msg("只回答869")], request_id="fast")]

    assert [d.text_delta for d in out if d.text_delta] == ["869"]
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_default_thinking_does_not_add_provider_specific_body(
    monkeypatch: pytest.MonkeyPatch, provider: LiteLLMProvider
) -> None:
    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _aiter([_Chunk(choices=[_Choice(delta=_Delta(content="ok"))])])

    monkeypatch.setattr(litellm, "acompletion", _fake)
    _ = [d async for d in provider.stream([_msg("hi")], request_id="default")]

    assert "extra_body" not in captured


@pytest.mark.parametrize("mode", ["enabled", "disabled"])
async def test_explicit_thinking_mode_is_preserved_for_stream_and_warmup(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    calls: list[dict] = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        if kwargs["stream"]:
            return _aiter([_Chunk(choices=[_Choice(delta=_Delta(content="ok"))])])
        return object()

    monkeypatch.setattr(litellm, "acompletion", _fake)
    configured = LiteLLMProvider(
        model="openai/deepseek-v4-flash",
        shared_http_client=False,
        thinking=mode,
    )

    _ = [d async for d in configured.stream([_msg("hi")], request_id=f"stream-{mode}")]
    assert await configured.warmup(timeout_s=1)

    assert len(calls) == 2
    assert all(
        call["extra_body"] == {"thinking": {"type": mode}}
        for call in calls
    )


async def test_ttft_ignores_empty_raw_chunks(
    monkeypatch: pytest.MonkeyPatch,
    provider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    chunks = [
        _Chunk(choices=[]),
        _Chunk(choices=[_Choice(delta=_Delta())]),
        _Chunk(choices=[_Choice(delta=_Delta(content="   "))]),
        _Chunk(choices=[_Choice(delta=_Delta(content="hello"))]),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="stop")]),
    ]

    async def _fake(**kwargs):
        return _aiter(chunks)

    monkeypatch.setattr(litellm, "acompletion", _fake)
    with caplog.at_level("INFO"):
        out = [d async for d in provider.stream([_msg("hi")], request_id="effective-text")]

    assert [d.text_delta for d in out if d.text_delta] == ["   ", "hello"]
    raw = [
        record.message
        for record in caplog.records
        if "litellm_stream_first_chunk" in record.message
    ]
    effective = [record.message for record in caplog.records if "litellm_timings" in record.message]
    assert len(raw) == 1
    assert len(effective) == 1
    assert "raw_chunks_before_effective=3" in effective[0]
    assert "kind=text" in effective[0]


@pytest.mark.parametrize(
    "reasoning_delta",
    [
        _Delta(reasoning_content="private chain of thought"),
        _Delta(provider_specific_fields={"reasoning_content": "private chain of thought"}),
    ],
)
async def test_reasoning_is_progress_activity_but_never_text(
    monkeypatch: pytest.MonkeyPatch,
    provider,
    reasoning_delta: _Delta,
    caplog: pytest.LogCaptureFixture,
) -> None:
    chunks = [
        _Chunk(choices=[_Choice(delta=reasoning_delta)]),
        _Chunk(choices=[_Choice(delta=_Delta(content="869"))]),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="stop")]),
    ]

    async def _fake(**kwargs):
        return _aiter(chunks)

    monkeypatch.setattr(litellm, "acompletion", _fake)
    with caplog.at_level("INFO"):
        out = [d async for d in provider.stream([_msg("只回答869")], request_id="reasoning")]

    assert [d.activity for d in out if d.activity] == [LLMActivityKind.REASONING]
    assert [d.text_delta for d in out if d.text_delta] == ["869"]
    assert all("private chain of thought" not in record.message for record in caplog.records)
    assert any("litellm_reasoning_activity" in record.message for record in caplog.records)
    assert any("reasoning_chunks=1" in record.message for record in caplog.records)


async def test_tool_call_fragment_counts_as_effective_delta(
    monkeypatch: pytest.MonkeyPatch,
    provider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    chunks = [
        _Chunk(choices=[]),
        _Chunk(
            choices=[
                _Choice(
                    delta=_Delta(
                        tool_calls=[
                            _ToolCallChunk(
                                index=0,
                                id="call-1",
                                function=_ToolCallFunc(name="delegate_to_coworker"),
                            )
                        ]
                    )
                )
            ]
        ),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="tool_calls")]),
    ]

    async def _fake(**kwargs):
        return _aiter(chunks)

    monkeypatch.setattr(litellm, "acompletion", _fake)
    with caplog.at_level("INFO"):
        out = [d async for d in provider.stream([_msg("hi")], request_id="effective-tool")]

    assert any(d.tool_call for d in out)
    effective = [record.message for record in caplog.records if "litellm_timings" in record.message]
    assert len(effective) == 1
    assert "raw_chunks_before_effective=1" in effective[0]
    assert "kind=tool_call" in effective[0]


async def test_stream_without_effective_delta_is_diagnosable(
    monkeypatch: pytest.MonkeyPatch,
    provider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    chunks = [
        _Chunk(choices=[]),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="stop")]),
    ]

    async def _fake(**kwargs):
        return _aiter(chunks)

    monkeypatch.setattr(litellm, "acompletion", _fake)
    with caplog.at_level("INFO"):
        out = [d async for d in provider.stream([_msg("hi")], request_id="empty")]

    assert any(d.finish is LLMFinishReason.STOP for d in out)
    assert not any("litellm_timings" in record.message for record in caplog.records)
    empty = [
        record.message
        for record in caplog.records
        if "litellm_stream_no_effective_delta" in record.message
    ]
    assert len(empty) == 1
    assert "raw_chunks=2" in empty[0]
    assert "finish_reason=stop" in empty[0]
    assert "outcome=completed" in empty[0]


async def test_request_includes_retries_and_api_base(monkeypatch: pytest.MonkeyPatch, provider) -> None:
    seen = {}

    async def _fake(**kwargs):
        seen.update(kwargs)
        return _aiter([_Chunk(choices=[_Choice(delta=_Delta(), finish_reason="stop")])])

    monkeypatch.setattr(litellm, "acompletion", _fake)
    out = [d async for d in provider.stream([_msg("hi")], request_id="r")]
    assert any(d.finish is LLMFinishReason.STOP for d in out)
    assert seen["api_base"] == "http://x/v1"
    assert seen["api_key"] == "k"
    assert seen["max_retries"] == 2


async def test_warmup_uses_small_non_streaming_request(
    monkeypatch: pytest.MonkeyPatch, provider
) -> None:
    seen = {}

    async def _fake(**kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(litellm, "acompletion", _fake)
    assert await provider.warmup(timeout_s=3)
    assert seen["stream"] is False
    assert seen["max_tokens"] == 1
    assert seen["timeout"] == 3


async def test_tool_call_buffered_across_chunks(monkeypatch: pytest.MonkeyPatch, provider) -> None:
    # Tool-call deltas arrive piecewise: id+name first, then arguments
    # split across two chunks.
    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(tool_calls=[
            _ToolCallChunk(
                index=0,
                id="call-1",
                function=_ToolCallFunc(name="delegate_to_coworker"),
            )
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
    assert tcs[0].name == "delegate_to_coworker"
    assert tcs[0].arguments == {"tz": "UTC"}
    finishes = [d.finish for d in out if d.finish is not None]
    assert finishes == [LLMFinishReason.TOOL_CALLS]


async def test_disabled_thinking_keeps_tool_activity_and_complete_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}
    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(tool_calls=[
            _ToolCallChunk(
                index=0,
                id="call-fast",
                function=_ToolCallFunc(name="get_current_time", arguments="{"),
            )
        ]))]),
        _Chunk(choices=[_Choice(delta=_Delta(tool_calls=[
            _ToolCallChunk(
                index=0,
                function=_ToolCallFunc(arguments='"timezone":"Asia/Shanghai"}'),
            )
        ]))]),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="tool_calls")]),
    ]

    async def _fake(**kwargs):
        seen.update(kwargs)
        return _aiter(chunks)

    monkeypatch.setattr(litellm, "acompletion", _fake)
    configured = LiteLLMProvider(
        model="openai/deepseek-v4-flash",
        shared_http_client=False,
        thinking="disabled",
    )

    out = [d async for d in configured.stream([_msg("上海几点")], request_id="tool-fast")]

    assert seen["extra_body"] == {"thinking": {"type": "disabled"}}
    # Fragment activity is intentionally heartbeat-rate-limited; the complete
    # ToolCall below must still contain every fragment.
    assert sum(d.activity is LLMActivityKind.TOOL_CALL for d in out) >= 1
    assert not any(d.activity is LLMActivityKind.REASONING for d in out)
    assert [d.tool_call for d in out if d.tool_call] == [
        ToolCall(
            id="call-fast",
            name="get_current_time",
            arguments={"timezone": "Asia/Shanghai"},
        )
    ]
    assert [d.finish for d in out if d.finish] == [LLMFinishReason.TOOL_CALLS]


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


async def test_response_aclose_called_on_normal_completion(
    monkeypatch: pytest.MonkeyPatch, provider
) -> None:
    """The provider must close the upstream stream once iteration ends so the
    HTTP connection returns to the pool / is torn down."""
    closed = {"v": False}

    class _Resp:
        def __init__(self, items):
            self._it = iter(items)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration from None

        async def aclose(self) -> None:
            closed["v"] = True

    chunks = [
        _Chunk(choices=[_Choice(delta=_Delta(content="hi"))]),
        _Chunk(choices=[_Choice(delta=_Delta(), finish_reason="stop")]),
    ]

    async def _fake(**kwargs):
        return _Resp(chunks)

    monkeypatch.setattr(litellm, "acompletion", _fake)
    async for _ in provider.stream([_msg("x")], request_id="r"):
        pass
    assert closed["v"] is True


async def test_response_aclose_called_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
    provider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the consumer cancels mid-stream (TCP close, RPC cancel), the
    provider must still call ``aclose()`` on the upstream — otherwise the
    HTTP request stays open and we keep paying for tokens nobody reads."""
    import asyncio

    closed = {"v": False}

    class _SlowResp:
        def __aiter__(self):
            return self

        async def __anext__(self):
            # Block forever — the consumer must cancel to escape.
            await asyncio.sleep(60)
            raise StopAsyncIteration

        async def aclose(self) -> None:
            closed["v"] = True

    async def _fake(**kwargs):
        return _SlowResp()

    monkeypatch.setattr(litellm, "acompletion", _fake)

    async def _consume() -> None:
        async for _ in provider.stream([_msg("x")], request_id="r"):
            pass

    with caplog.at_level("INFO"):
        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.05)  # let it block on __anext__
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed["v"] is True
    empty = [
        record.message
        for record in caplog.records
        if "litellm_stream_no_effective_delta" in record.message
    ]
    assert len(empty) == 1
    assert "raw_chunks=0" in empty[0]
    assert "outcome=interrupted" in empty[0]


async def test_count_tokens_falls_back_when_litellm_raises(
    monkeypatch: pytest.MonkeyPatch, provider
) -> None:
    def _boom(**kwargs):
        raise RuntimeError("no model in cost map")

    monkeypatch.setattr(litellm, "token_counter", _boom)
    # 30-char content → 30 // 3 == 10 tokens approx
    n = await provider.count_tokens([_msg("a" * 30)])
    assert n >= 1
