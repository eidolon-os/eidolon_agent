"""CrisisHandler.handle — returns canned reply and publishes audit event."""

from __future__ import annotations

import asyncio

import pytest

from eidolon_agent.domain.guardrails import CrisisHandler

pytestmark = pytest.mark.functional


async def test_crisis_returns_zh_response_and_resources() -> None:
    handler = CrisisHandler()
    resp = await handler.handle(companion_id="inst-1", owner_id="alice")
    assert "听到你了" in resp.text  # zh-CN canned reply
    assert resp.suppress_memory_write is True
    # Resources present and contain phone-style markers.
    assert resp.crisis_resources
    assert any("400" in r or "010-" in r for r in resp.crisis_resources)


async def test_crisis_publishes_audit_event(event_bus) -> None:
    received: list = []

    async def _handler(ev) -> None:
        received.append(ev)

    await event_bus.subscribe("agent.guardrail.crisis.inst-42", _handler)
    handler = CrisisHandler(event_bus=event_bus)
    await handler.handle(companion_id="inst-42", owner_id="alice")
    await asyncio.sleep(0)
    assert len(received) == 1
    assert received[0].payload["owner_id"] == "alice"
    assert received[0].payload["locale"] == "zh-CN"


async def test_crisis_without_bus_still_returns_response() -> None:
    handler = CrisisHandler(event_bus=None)
    resp = await handler.handle(companion_id="inst-1", owner_id="bob")
    assert resp.text
    assert resp.suppress_memory_write is True
