"""InMemoryEventBus + InMemoryKVStore — used everywhere as the test backend."""

from __future__ import annotations

import asyncio

import pytest

from eidolon_agent.core.errors import ConflictError
from eidolon_agent.core.types.event import Event
from eidolon_agent.infra.events import InMemoryEventBus, InMemoryKVStore

pytestmark = pytest.mark.unit


# ---- EventBus -----------------------------------------------------------


async def test_publish_then_subscribe_does_not_replay() -> None:
    bus = InMemoryEventBus()
    received: list = []

    async def _h(ev) -> None:
        received.append(ev)

    await bus.publish(Event(subject="a.b", payload={"k": 1}, source="t"))
    await bus.subscribe("a.b", _h)
    await bus.publish(Event(subject="a.b", payload={"k": 2}, source="t"))
    await asyncio.sleep(0)
    # Only the second event reaches the subscriber.
    assert [e.payload["k"] for e in received] == [2]


async def test_wildcard_subscription_matches_segments() -> None:
    bus = InMemoryEventBus()
    received: list = []

    async def _h(ev) -> None:
        received.append(ev.subject)

    await bus.subscribe("foo.bar.>", _h)  # JetStream-style prefix wildcard
    await bus.publish(Event(subject="foo.bar.baz", payload={}, source="t"))
    await bus.publish(Event(subject="foo.bar.x.y", payload={}, source="t"))
    await bus.publish(Event(subject="other.bar.baz", payload={}, source="t"))
    await asyncio.sleep(0)  # let handlers run
    assert sorted(received) == ["foo.bar.baz", "foo.bar.x.y"]


async def test_unsubscribe_stops_delivery() -> None:
    bus = InMemoryEventBus()
    received: list = []

    async def _h(ev) -> None:
        received.append(ev)

    unsub = await bus.subscribe("a.b", _h)
    await bus.publish(Event(subject="a.b", payload={"k": 1}, source="t"))
    await asyncio.sleep(0)
    await unsub()
    await bus.publish(Event(subject="a.b", payload={"k": 2}, source="t"))
    await asyncio.sleep(0)
    assert len(received) == 1


# ---- KVStore -----------------------------------------------------------


async def test_kv_put_get_roundtrip() -> None:
    kv = InMemoryKVStore("BUCKET")
    await kv.put("k", b"value")
    assert await kv.get("k") == b"value"


async def test_kv_missing_returns_none() -> None:
    kv = InMemoryKVStore("BUCKET")
    assert await kv.get("ghost") is None


async def test_kv_delete_removes_key() -> None:
    kv = InMemoryKVStore("BUCKET")
    await kv.put("k", b"v")
    await kv.delete("k")
    assert await kv.get("k") is None


async def test_kv_cas_with_correct_revision() -> None:
    kv = InMemoryKVStore("BUCKET")
    rev1 = await kv.put("k", b"v1")
    rev2 = await kv.cas("k", b"v2", expected_revision=rev1)
    assert rev2 != rev1
    assert await kv.get("k") == b"v2"


async def test_kv_cas_with_stale_revision_raises_conflict() -> None:
    kv = InMemoryKVStore("BUCKET")
    rev1 = await kv.put("k", b"v1")
    await kv.cas("k", b"v2", expected_revision=rev1)  # advance
    with pytest.raises(ConflictError):
        await kv.cas("k", b"v3", expected_revision=rev1)  # stale


async def test_kv_keys_filters_by_prefix() -> None:
    kv = InMemoryKVStore("BUCKET")
    await kv.put("user.a", b"x")
    await kv.put("user.b", b"y")
    await kv.put("other", b"z")
    keys = await kv.keys("user.")
    assert sorted(keys) == ["user.a", "user.b"]
