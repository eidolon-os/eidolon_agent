"""Name / area / device-type addressing for the LLM-facing smart-home tools.

The same rules a person would assume, and the rules interpreter follows: an
exact name or alias first; otherwise area and type narrow the home; without an
area the speaker's room is preferred; several matches are ambiguous unless the
caller asked for all of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from eidolon_sdk.biz.smarthome import Device, Registry


@dataclass(frozen=True, slots=True)
class Address:
    name: str | None = None
    area: str | None = None
    device_type: str | None = None
    all: bool = False

    @property
    def empty(self) -> bool:
        return not (self.name or self.area or self.device_type)


@dataclass(frozen=True, slots=True)
class Resolution:
    status: Literal["resolved", "ambiguous", "not_found"]
    devices: tuple[Device, ...] = ()


def resolve_address(registry: Registry, address: Address, *, origin_area: str | None) -> Resolution:
    pool = list(registry.devices)
    if address.name:
        key = _key(address.name)
        pool = [
            d
            for d in pool
            if key == _key(d.device_id) or key in {_key(d.name), *(_key(a) for a in d.aliases)}
        ]
    if address.area:
        key = _key(address.area)
        area = next((a for a in registry.areas if key in (a.area_id, _key(a.name))), None)
        if area is None:
            return Resolution("not_found")
        pool = [d for d in pool if d.area_id == area.area_id]
    if address.device_type:
        pool = [d for d in pool if d.type == address.device_type]
    if address.empty or not pool:
        return Resolution("not_found")
    if len(pool) == 1 or address.all:
        return Resolution("resolved", tuple(pool))
    if not address.area and origin_area is not None:
        local = [d for d in pool if d.area_id == origin_area]
        if len(local) == 1:
            return Resolution("resolved", tuple(local))
        if local:
            pool = local
    return Resolution("ambiguous", tuple(pool))


def _key(value: str) -> str:
    return "".join(str(value).split()).casefold()
