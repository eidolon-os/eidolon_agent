"""Resolve user-facing body-device names to a concrete device."""

from __future__ import annotations

from eidolon_sdk.biz.body import BodyDevice

from eidolon_agent.domain.body_control.errors import BodyDeviceAmbiguous, BodyDeviceNotFound

_CURRENT_DEVICE_ALIASES = {
    "",
    "this",
    "this device",
    "current",
    "current device",
    "这个设备",
    "当前设备",
    "本机",
}


def resolve_body_device(
    devices: list[BodyDevice],
    target: str,
    *,
    source_device_id: str | None = None,
) -> BodyDevice:
    key = _normalize(target)
    if key in _CURRENT_DEVICE_ALIASES:
        for device in devices:
            if device.is_current_device or (
                source_device_id and device.device_id == source_device_id
            ):
                return device
        raise BodyDeviceNotFound("current body device is unknown")

    exact = [device for device in devices if key in _device_keys(device)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise BodyDeviceAmbiguous(target, [device.device_id for device in exact])

    partial = [device for device in devices if _matches_partial(key, device)]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise BodyDeviceAmbiguous(target, [device.device_id for device in partial])

    raise BodyDeviceNotFound(f"body device not found: {target}")


def _device_keys(device: BodyDevice) -> set[str]:
    values = {device.device_id, device.name, *device.aliases}
    return {_normalize(value) for value in values if value}


def _matches_partial(key: str, device: BodyDevice) -> bool:
    if not key:
        return False
    return any(key in candidate for candidate in _device_keys(device))


def _normalize(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())
