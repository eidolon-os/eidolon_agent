"""NatsEventBus — connection lifecycle & publish routing (NATS SDK mocked).

The real NATS client is replaced with stubs so we can verify:
- lazy connection via ``connect()``
- ``publish()`` routes ``is_persistent`` subjects through JetStream
- ``publish()`` routes transient subjects through core NATS
- ``request()`` translates ``nats.errors.TimeoutError`` → ``asyncio.TimeoutError``
- connection failure is wrapped as ``NatsUnavailableError``

A real NATS integration test belongs under ``smoke``; this is the unit
contract.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import nats.errors
import pytest
from eidolon_sdk.memory import conversation_turn_subject

from eidolon_agent.core.errors import NatsUnavailableError
from eidolon_agent.core.types.event import Event
from eidolon_agent.infra.events import NatsEventBus

pytestmark = pytest.mark.unit


def _make_bus(monkeypatch: pytest.MonkeyPatch) -> tuple[NatsEventBus, MagicMock, MagicMock]:
    """Build a NatsEventBus whose ``nats.connect`` returns a stub client.

    Returns (bus, fake_nc, fake_js) so tests can assert on calls.
    """
    fake_js = MagicMock()
    fake_js.add_stream = AsyncMock()
    fake_js.publish = AsyncMock()
    fake_js.subscribe = AsyncMock()
    fake_js.create_key_value = AsyncMock()

    fake_nc = MagicMock()
    fake_nc.is_connected = True
    fake_nc.jetstream = MagicMock(return_value=fake_js)
    fake_nc.publish = AsyncMock()
    fake_nc.request = AsyncMock()
    fake_nc.drain = AsyncMock()
    fake_nc.subscribe = AsyncMock()

    async def _connect(*args, **kwargs):
        return fake_nc

    monkeypatch.setattr("eidolon_agent.infra.events.nats_bus.nats.connect", _connect)
    bus = NatsEventBus("nats://test:4222")
    return bus, fake_nc, fake_js


async def test_connect_failure_wrapped_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail(*args, **kwargs):
        raise OSError("ECONNREFUSED")

    monkeypatch.setattr("eidolon_agent.infra.events.nats_bus.nats.connect", _fail)
    bus = NatsEventBus("nats://unreachable:4222")
    with pytest.raises(NatsUnavailableError, match="connect"):
        await bus.connect()


async def test_publish_transient_subject_uses_core_nats(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, fake_nc, fake_js = _make_bus(monkeypatch)
    await bus.publish(Event(subject="agent.turn.completed.x", payload={"k": 1}, source="t"))
    fake_nc.publish.assert_awaited_once()
    fake_js.publish.assert_not_called()


async def test_publish_persistent_subject_uses_jetstream(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, fake_nc, fake_js = _make_bus(monkeypatch)
    await bus.publish(
        Event(subject=conversation_turn_subject("default.alice.default"), payload={}, source="t")
    )
    fake_js.publish.assert_awaited_once()
    fake_nc.publish.assert_not_called()


async def test_publish_explicit_persistent_flag_forces_jetstream(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, _fake_nc, fake_js = _make_bus(monkeypatch)
    # transient subject normally, but persistent=True forces JetStream
    await bus.publish(
        Event(subject="agent.turn.completed.x", payload={}, source="t"),
        persistent=True,
    )
    fake_js.publish.assert_awaited_once()


async def test_publish_with_msg_id_sets_dedup_header(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, _fake_nc, fake_js = _make_bus(monkeypatch)
    await bus.publish(
        Event(
            subject=conversation_turn_subject("default.alice.default"),
            payload={},
            source="t",
            metadata={"msg_id": "turn-42"},
        )
    )
    # Inspect the headers kwarg passed to JetStream.
    call = fake_js.publish.await_args
    headers = call.kwargs["headers"]
    assert headers["Nats-Msg-Id"] == "turn-42"


async def test_request_translates_nats_timeout_to_asyncio_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, fake_nc, _ = _make_bus(monkeypatch)
    fake_nc.request = AsyncMock(side_effect=nats.errors.TimeoutError())
    with pytest.raises(asyncio.TimeoutError):
        await bus.request("agent.svc.echo", {"k": 1}, timeout_s=0.1)


async def test_health_reflects_connection_state(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, _, _ = _make_bus(monkeypatch)
    assert await bus.health() is False  # not connected yet
    await bus.connect()
    assert await bus.health() is True


async def test_connect_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    n = {"calls": 0}

    fake_js = MagicMock()
    fake_js.add_stream = AsyncMock()
    fake_nc = MagicMock()
    fake_nc.is_connected = True
    fake_nc.jetstream = MagicMock(return_value=fake_js)

    async def _connect(*args, **kwargs):
        n["calls"] += 1
        return fake_nc

    monkeypatch.setattr("eidolon_agent.infra.events.nats_bus.nats.connect", _connect)
    bus = NatsEventBus("nats://t:4222")
    await bus.connect()
    await bus.connect()
    assert n["calls"] == 1
