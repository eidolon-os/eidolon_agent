"""SqlPersonaInstanceRepository — CRUD over the persona_instances table.

The repository is a thin adapter between ``PersonaInstance`` (pydantic) and
``PersonaInstanceRow`` (ORM). Tests verify round-tripping, version updates,
and tenant/user scoping on get/delete.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from eidolon_agent.domain.personas.types import (
    BehavioralKnob,
    IdentityCore,
    PersonaInstance,
    PersonaMetadata,
)

pytestmark = pytest.mark.unit


def _instance(
    *,
    instance_id: str = "inst-1",
    tenant_id: str = "t",
    user_id: str = "alice",
    overlay_version: int = 1,
) -> PersonaInstance:
    now = datetime.now(timezone.utc)
    return PersonaInstance(
        instance_id=instance_id,
        tenant_id=tenant_id,
        user_id=user_id,
        origin_template_id="tpl-1",
        origin_template_revision=1,
        overlay_version=overlay_version,
        created_at=now,
        updated_at=now,
        metadata=PersonaMetadata(
            template_id="tpl-1", archetype="archetype-x", name="name-x"
        ),
        identity_core=IdentityCore(),
        behavioral_knobs={"intimacy": BehavioralKnob(current=0.5)},
    )


async def test_upsert_then_load_round_trips(uow_factory) -> None:
    inst = _instance()
    async with uow_factory() as uow:
        await uow.persona_instances.upsert(inst)
        await uow.commit()
    async with uow_factory() as uow:
        loaded = await uow.persona_instances.load("t", "alice", "inst-1")
    assert loaded == inst


async def test_upsert_overwrites_overlay_version(uow_factory) -> None:
    async with uow_factory() as uow:
        await uow.persona_instances.upsert(_instance(overlay_version=1))
        await uow.commit()
    async with uow_factory() as uow:
        await uow.persona_instances.upsert(_instance(overlay_version=7))
        await uow.commit()
    async with uow_factory() as uow:
        loaded = await uow.persona_instances.load("t", "alice", "inst-1")
    assert loaded.overlay_version == 7


async def test_get_respects_tenant_user_scope(uow_factory) -> None:
    async with uow_factory() as uow:
        await uow.persona_instances.upsert(_instance(tenant_id="t-a", user_id="alice"))
        await uow.commit()
    async with uow_factory() as uow:
        # Wrong tenant returns None
        assert await uow.persona_instances.get("t-b", "alice", "inst-1") is None
        # Wrong user returns None
        assert await uow.persona_instances.get("t-a", "bob", "inst-1") is None
        # Correct triple returns the row
        assert await uow.persona_instances.get("t-a", "alice", "inst-1") is not None


async def test_load_unknown_raises_not_found(uow_factory) -> None:
    from eidolon_agent.core.errors import NotFoundError

    async with uow_factory() as uow:
        with pytest.raises(NotFoundError):
            await uow.persona_instances.load("t", "alice", "ghost")


async def test_list_all_returns_all_rows(uow_factory) -> None:
    async with uow_factory() as uow:
        await uow.persona_instances.upsert(_instance(instance_id="i1"))
        await uow.persona_instances.upsert(_instance(instance_id="i2"))
        await uow.commit()
    async with uow_factory() as uow:
        rows = await uow.persona_instances.list_all()
    assert {r.instance_id for r in rows} == {"i1", "i2"}


async def test_delete_removes_the_row(uow_factory) -> None:
    async with uow_factory() as uow:
        await uow.persona_instances.upsert(_instance())
        await uow.commit()
    async with uow_factory() as uow:
        await uow.persona_instances.delete("t", "alice", "inst-1")
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.persona_instances.get("t", "alice", "inst-1") is None


async def test_touch_last_active_updates_timestamp(uow_factory) -> None:
    async with uow_factory() as uow:
        await uow.persona_instances.upsert(_instance())
        await uow.commit()
    async with uow_factory() as uow:
        await uow.persona_instances.touch_last_active("inst-1")
        await uow.commit()
    # The fetched row should have last_active_at set; check via direct ORM read.
    from sqlalchemy import select

    from eidolon_agent.infra.persistence.models import PersonaInstanceRow

    async with uow_factory() as uow:
        row = (
            await uow._session.execute(  # type: ignore[attr-defined]
                select(PersonaInstanceRow).where(PersonaInstanceRow.id == "inst-1")
            )
        ).scalar_one()
    assert row.last_active_at is not None
