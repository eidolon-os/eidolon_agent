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


async def test_kv_keys_skips_expired_entries() -> None:
    """TTL'd entry that has lapsed should not show up in keys()."""
    kv = InMemoryKVStore("BUCKET")
    await kv.put("alive", b"x")
    await kv.put("dead", b"y", ttl_s=1)
    # Manually expire by stamping a past expires_at.
    kv._store["dead"].expires_at = 0.0
    assert await kv.keys() == ["alive"]


async def test_kv_get_clears_expired_entry() -> None:
    kv = InMemoryKVStore("BUCKET")
    await kv.put("k", b"v", ttl_s=1)
    kv._store["k"].expires_at = 0.0
    assert await kv.get("k") is None
    # Entry was actively evicted from the store.
    assert "k" not in kv._store


async def test_kv_watch_yields_put_and_delete_events() -> None:
    """``watch()`` is an async generator: yields (key, value, rev) on put,
    (key, None, -1) on delete, until the consumer stops iterating."""
    kv = InMemoryKVStore("BUCKET")

    async def _consume(limit: int) -> list:
        out: list = []
        async for evt in kv.watch("user.>"):
            out.append(evt)
            if len(out) >= limit:
                break
        return out

    # Start the watcher in the background, then publish updates.
    consumer = asyncio.create_task(_consume(2))
    await asyncio.sleep(0)  # let the watcher register
    await kv.put("user.a", b"hello")
    await kv.delete("user.a")
    events = await asyncio.wait_for(consumer, timeout=1)
    assert events[0][0] == "user.a"
    assert events[0][1] == b"hello"
    assert events[0][2] >= 1  # revision
    assert events[1][0] == "user.a"
    assert events[1][1] is None  # delete
    assert events[1][2] == -1


async def test_kv_watch_only_matches_pattern() -> None:
    kv = InMemoryKVStore("BUCKET")

    received: list = []

    async def _consume() -> None:
        async for evt in kv.watch("alpha.>"):
            received.append(evt[0])
            if len(received) >= 1:
                return

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0)
    await kv.put("other.x", b"1")  # should NOT match
    await kv.put("alpha.x", b"1")  # should match
    await asyncio.wait_for(task, timeout=1)
    assert received == ["alpha.x"]


# ---- EventBus request/reply ----------------------------------------------


async def test_event_bus_request_returns_responder_reply() -> None:
    bus = InMemoryEventBus()

    async def _responder(payload: dict) -> dict:
        return {"echo": payload.get("ping", "?")}

    bus.register_responder("svc.echo", _responder)
    resp = await bus.request("svc.echo", {"ping": "pong"})
    assert resp == {"echo": "pong"}


async def test_event_bus_request_unknown_subject_times_out() -> None:
    bus = InMemoryEventBus()
    with pytest.raises(asyncio.TimeoutError):
        await bus.request("svc.nobody", {}, timeout_s=0.05)


async def test_event_bus_health_is_true() -> None:
    assert await InMemoryEventBus().health() is True


# ---- queue group load-balancing ------------------------------------------


async def test_queue_group_delivers_to_one_member_per_event() -> None:
    """Three subscribers in the same queue_group: each event reaches only
    one of them. Independent subscribers (no group) still all receive."""
    bus = InMemoryEventBus()
    counts: dict[str, int] = {"a": 0, "b": 0, "c": 0, "watcher": 0}

    async def _make(name: str):
        async def _h(_ev) -> None:
            counts[name] += 1

        return _h

    await bus.subscribe("svc.work", await _make("a"), queue_group="workers")
    await bus.subscribe("svc.work", await _make("b"), queue_group="workers")
    await bus.subscribe("svc.work", await _make("c"), queue_group="workers")
    await bus.subscribe("svc.work", await _make("watcher"))

    for _ in range(4):
        await bus.publish(Event(subject="svc.work", payload={}, source="t"))
    await asyncio.sleep(0)

    worker_total = counts["a"] + counts["b"] + counts["c"]
    assert worker_total == 4  # one worker handled each event
    assert counts["watcher"] == 4  # independent subscriber got all 4


# ---- handler failure isolation -------------------------------------------


async def test_handler_exception_is_logged_not_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing handler must not block other handlers and must surface a
    WARNING log including the subject — silent swallowing makes debugging
    impossible.
    """
    bus = InMemoryEventBus()
    survivors: list[str] = []

    async def _broken(_ev) -> None:
        raise RuntimeError("kaboom")

    async def _ok(ev) -> None:
        survivors.append(ev.subject)

    await bus.subscribe("svc.x", _broken)
    await bus.subscribe("svc.x", _ok)

    with caplog.at_level("WARNING", logger="eidolon_agent.infra.events.adapters.inmem"):
        await bus.publish(Event(subject="svc.x", payload={}, source="t"))
        await asyncio.sleep(0)
        # Give the broken handler's task a chance to surface the exception.
        await asyncio.sleep(0)

    # The healthy handler still received the event…
    assert survivors == ["svc.x"]
    # …and the broken one logged a WARNING that mentions the subject.
    matching = [r for r in caplog.records if "svc.x" in r.getMessage()]
    assert matching, f"expected WARNING mentioning subject, got {caplog.records!r}"
    assert any(r.levelname == "WARNING" for r in matching)


async def test_delivery_task_set_drains_after_handlers_finish() -> None:
    """The bus retains strong refs to in-flight tasks (so GC can't reap them
    mid-handler) and drops them once each task completes."""
    bus = InMemoryEventBus()

    async def _h(_ev) -> None:
        pass

    await bus.subscribe("svc.y", _h)
    await bus.publish(Event(subject="svc.y", payload={}, source="t"))
    # Right after publish but before the handler has run, the task set should
    # have one entry (we deliberately kept the ref).
    assert len(bus._delivery_tasks) >= 1
    # Let the handler run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # done_callback should have removed it.
    assert bus._delivery_tasks == set()
