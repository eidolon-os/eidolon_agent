"""LLMRouter — provider selection, error normalization."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from eidolon_agent.core.errors import LLMUnavailableError
from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.infra.llm import LLMRouter

pytestmark = pytest.mark.unit


class _StubProvider:
    def __init__(
        self,
        *,
        deltas: list[LLMDelta] | None = None,
        raise_exc: Exception | None = None,
        raise_after: Exception | None = None,
    ) -> None:
        self._deltas = deltas or [LLMDelta(text_delta="ok"), LLMDelta(finish=LLMFinishReason.STOP)]
        self._exc = raise_exc
        self._raise_after = raise_after

    async def stream(self, messages, *, tools=None, model=None, temperature=0.7, max_tokens=None, request_id) -> AsyncIterator[LLMDelta]:
        if self._exc is not None:
            raise self._exc
        for d in self._deltas:
            yield d
        if self._raise_after is not None:
            raise self._raise_after

    async def count_tokens(self, messages):
        return 1


def test_router_rejects_unknown_default() -> None:
    with pytest.raises(ValueError, match="default model"):
        LLMRouter(providers={"x": _StubProvider()}, default="y")


async def test_router_uses_default_when_no_model_given() -> None:
    p = _StubProvider(deltas=[LLMDelta(text_delta="from-default")])
    router = LLMRouter(providers={"def": p}, default="def")
    chunks = []
    async for d in router.stream([], request_id="r"):
        chunks.append(d.text_delta)
    assert chunks == ["from-default"]


async def test_router_picks_named_provider() -> None:
    p_a = _StubProvider(deltas=[LLMDelta(text_delta="A")])
    p_b = _StubProvider(deltas=[LLMDelta(text_delta="B")])
    router = LLMRouter(providers={"a": p_a, "b": p_b}, default="a")
    chunks = [d.text_delta async for d in router.stream([], model="b", request_id="r")]
    assert chunks == ["B"]


async def test_router_falls_back_to_default_for_unknown_model() -> None:
    p = _StubProvider(deltas=[LLMDelta(text_delta="default-served")])
    router = LLMRouter(providers={"def": p}, default="def")
    chunks = [d.text_delta async for d in router.stream([], model="nope", request_id="r")]
    assert chunks == ["default-served"]


async def test_router_wraps_provider_exception_as_llm_unavailable() -> None:
    p = _StubProvider(raise_exc=RuntimeError("oom"))
    router = LLMRouter(providers={"x": p}, default="x")
    with pytest.raises(LLMUnavailableError, match="provider x failed: oom"):
        async for _ in router.stream([], request_id="r"):
            pass


async def test_router_preserves_llm_unavailable() -> None:
    p = _StubProvider(raise_exc=LLMUnavailableError("upstream busy"))
    router = LLMRouter(providers={"x": p}, default="x")
    with pytest.raises(LLMUnavailableError, match="upstream busy"):
        async for _ in router.stream([], request_id="r"):
            pass


async def test_router_uses_fallback_before_first_chunk() -> None:
    primary = _StubProvider(raise_exc=LLMUnavailableError("upstream busy"))
    fallback = _StubProvider(deltas=[LLMDelta(text_delta="fallback")])
    router = LLMRouter(
        providers={"primary": primary, "fallback": fallback},
        default="primary",
        fallback_models=["fallback"],
    )
    chunks = [d.text_delta async for d in router.stream([], request_id="r")]
    assert chunks == ["fallback"]


async def test_router_does_not_fallback_after_stream_started() -> None:
    primary = _StubProvider(
        deltas=[LLMDelta(text_delta="partial")],
        raise_after=LLMUnavailableError("stream reset"),
    )
    fallback = _StubProvider(deltas=[LLMDelta(text_delta="fallback")])
    router = LLMRouter(
        providers={"primary": primary, "fallback": fallback},
        default="primary",
        fallback_models=["fallback"],
    )
    seen = []
    with pytest.raises(LLMUnavailableError, match="stream reset"):
        async for d in router.stream([], request_id="r"):
            seen.append(d.text_delta)
    assert seen == ["partial"]


async def test_count_tokens_uses_default_provider() -> None:
    router = LLMRouter(providers={"x": _StubProvider()}, default="x")
    assert await router.count_tokens([]) == 1
