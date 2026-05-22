"""SqlDeviceRepository — register, revoke, is_revoked, touch_last_seen."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


async def test_register_then_is_revoked_false(uow_factory) -> None:
    async with uow_factory() as uow:
        await uow.devices.register(
            device_id="dev-1", tenant_id="t", user_id="u",
            token_hash="hash", scopes=["device"],
        )
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.devices.is_revoked("dev-1") is False


async def test_revoke_marks_device_revoked(uow_factory) -> None:
    async with uow_factory() as uow:
        await uow.devices.register(
            device_id="dev-2", tenant_id="t", user_id="u",
            token_hash="h", scopes=[],
        )
        await uow.devices.revoke("dev-2")
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.devices.is_revoked("dev-2") is True


async def test_is_revoked_for_unknown_device_returns_true(uow_factory) -> None:
    # Convention: missing device is treated as revoked (safer default).
    async with uow_factory() as uow:
        assert await uow.devices.is_revoked("never-registered") is True


async def test_revoke_unknown_is_noop(uow_factory) -> None:
    async with uow_factory() as uow:
        await uow.devices.revoke("ghost")
        await uow.commit()


async def test_revoke_is_idempotent(uow_factory) -> None:
    async with uow_factory() as uow:
        await uow.devices.register(
            device_id="dev-3", tenant_id="t", user_id="u",
            token_hash="h", scopes=[],
        )
        await uow.devices.revoke("dev-3")
        await uow.commit()
    # Second revoke should not raise and should not move the revoked_at.
    async with uow_factory() as uow:
        await uow.devices.revoke("dev-3")
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.devices.is_revoked("dev-3") is True


async def test_touch_last_seen_updates_timestamp(uow_factory) -> None:
    from eidolon_agent.infra.persistence.models import DeviceRow

    async with uow_factory() as uow:
        await uow.devices.register(
            device_id="dev-4", tenant_id="t", user_id="u",
            token_hash="h", scopes=[],
        )
        await uow.devices.touch_last_seen("dev-4")
        await uow.commit()
    async with uow_factory() as uow:
        row = await uow._session.get(DeviceRow, "dev-4")  # type: ignore[attr-defined]
    assert row.last_seen_at is not None
