"""Hot-path cache for body-device directory lookups."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from eidolon_sdk.biz.body import BodyDevice

from eidolon_agent.domain.body_control.ports import BodyDeviceStorePort


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    devices: tuple[BodyDevice, ...]


class CachedBodyDeviceStore(BodyDeviceStorePort):
    """Small TTL cache around the durable/runtime device directory.

    The wrapped store merges durable device facts from eidolon_data with runtime
    reachability from Hub. That merge is on the hot path for body tools, so this
    cache keeps repeated calls such as resolve->control from hitting SQLite and
    Hub every time.
    """

    def __init__(
        self,
        wrapped: BodyDeviceStorePort,
        *,
        ttl_s: float = 3.0,
        max_entries: int = 256,
    ) -> None:
        self._wrapped = wrapped
        self._ttl_s = max(0.0, ttl_s)
        self._max_entries = max(1, max_entries)
        self._entries: dict[tuple[str, str, str], _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def list_devices(
        self,
        *,
        owner_id: str,
        companion_id: str,
        source_device_id: str | None = None,
    ) -> list[BodyDevice]:
        if self._ttl_s <= 0:
            return await self._wrapped.list_devices(
                owner_id=owner_id,
                companion_id=companion_id,
                source_device_id=source_device_id,
            )
        key = (owner_id, companion_id, source_device_id or "")
        now = time.monotonic()
        async with self._lock:
            cached = self._entries.get(key)
            if cached is not None and cached.expires_at > now:
                return list(cached.devices)
        devices = await self._wrapped.list_devices(
            owner_id=owner_id,
            companion_id=companion_id,
            source_device_id=source_device_id,
        )
        async with self._lock:
            if len(self._entries) >= self._max_entries and key not in self._entries:
                self._entries.pop(next(iter(self._entries)))
            self._entries[key] = _CacheEntry(
                expires_at=time.monotonic() + self._ttl_s,
                devices=tuple(devices),
            )
        return devices

    async def invalidate(
        self,
        *,
        owner_id: str | None = None,
        companion_id: str | None = None,
        device_id: str | None = None,
    ) -> None:
        async with self._lock:
            if owner_id is None and companion_id is None and device_id is None:
                self._entries.clear()
                return
            for key, entry in list(self._entries.items()):
                key_owner, key_companion, _source = key
                if owner_id is not None and key_owner != owner_id:
                    continue
                if companion_id is not None and key_companion != companion_id:
                    continue
                if device_id is not None and all(
                    device.device_id != device_id for device in entry.devices
                ):
                    continue
                self._entries.pop(key, None)
