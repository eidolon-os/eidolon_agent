"""ContextCompiler: pruning, concurrent providers, degraded reporting."""

import asyncio

import pytest

from eidolon_agent.context.compiler import ContextCompiler
from eidolon_agent.core.ports.context import ProviderContext
from eidolon_agent.core.types.context import ContextSegment, SegmentType, SegmentWeight
from tests.conftest import make_turn_input


class _Stub:
    def __init__(self, name, weight, tokens, delay_s=0.0):
        self.name = name
        self.segment_type = SegmentType.HISTORY
        self.default_weight = weight
        self.soft_timeout_s = 0.1
        self._tokens = tokens
        self._delay = delay_s

    async def provide(self, ctx: ProviderContext):
        if self._delay:
            await asyncio.sleep(self._delay)
        return [
            ContextSegment(
                type=self.segment_type,
                weight=self.default_weight,
                content=f"{self.name}-payload",
                tokens=self._tokens,
                source=self.name,
            )
        ]


@pytest.mark.asyncio
async def test_pruning_drops_lowest_weight_first():
    c = ContextCompiler(
        providers=[
            _Stub("low", SegmentWeight.LOW, 80),
            _Stub("hi", SegmentWeight.HIGH, 80),
            _Stub("crit", SegmentWeight.CRITICAL, 80),
        ],
        max_token_budget=200,
    )
    result = await c.compile(make_turn_input(""))
    sources = {s.source for s in result.segments}
    assert "crit" in sources and "hi" in sources
    assert "low" not in sources
    assert result.pruned_count == 1


@pytest.mark.asyncio
async def test_soft_timeout_marks_degraded():
    class _Slow(_Stub):
        soft_timeout_s = 0.02

    c = ContextCompiler(providers=[_Slow("slow", SegmentWeight.HIGH, 10, delay_s=0.5)])
    result = await c.compile(make_turn_input("hi"))
    assert "slow" in result.degraded_providers


@pytest.mark.asyncio
async def test_critical_segments_preserved_over_budget():
    c = ContextCompiler(
        providers=[
            _Stub("c1", SegmentWeight.CRITICAL, 100),
            _Stub("c2", SegmentWeight.CRITICAL, 100),
        ],
        max_token_budget=50,
    )
    result = await c.compile(make_turn_input(""))
    assert len(result.segments) == 2  # nothing dropped — all critical
    assert result.total_tokens > 50
