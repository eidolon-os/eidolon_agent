from __future__ import annotations

import asyncio

import pytest
from eidolon_sdk.biz.body import BodyDevice

from eidolon_agent.domain.body_control.directory import CachedBodyDeviceStore


@pytest.mark.asyncio
async def test_cached_body_device_store_reuses_hot_lookup() -> None:
    wrapped = _CountingStore()
    store = CachedBodyDeviceStore(wrapped, ttl_s=30.0)

    first = await store.list_devices(
        owner_id="owner-1",
        companion_id="companion-1",
        source_device_id="admin-console-1",
    )
    second = await store.list_devices(
        owner_id="owner-1",
        companion_id="companion-1",
        source_device_id="admin-console-1",
    )

    assert [device.device_id for device in first] == ["box-1"]
    assert [device.device_id for device in second] == ["box-1"]
    assert wrapped.calls == 1


@pytest.mark.asyncio
async def test_cached_body_device_store_invalidates_by_device_id() -> None:
    wrapped = _CountingStore()
    store = CachedBodyDeviceStore(wrapped, ttl_s=30.0)
    await store.list_devices(
        owner_id="owner-1",
        companion_id="companion-1",
        source_device_id=None,
    )

    await store.invalidate(device_id="box-1")
    await store.list_devices(
        owner_id="owner-1",
        companion_id="companion-1",
        source_device_id=None,
    )

    assert wrapped.calls == 2


class _CountingStore:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = asyncio.Lock()

    async def list_devices(
        self,
        *,
        owner_id: str,
        companion_id: str,
        source_device_id: str | None = None,
    ) -> list[BodyDevice]:
        async with self._lock:
            self.calls += 1
        return [
            BodyDevice(
                device_id="box-1",
                name="box-1",
                aliases=("小王",),
                status="online_control",
                is_current_device=source_device_id == "box-1",
            )
        ]
