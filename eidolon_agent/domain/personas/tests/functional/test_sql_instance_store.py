"""SqlPersonaInstanceStore — exercises the production storage path.

Sanity-checks the high-level lifecycle: create_from_template → load → save
(with overlay_version bump) → list_all → delete. Plus save_with_history for
the single-TX evolution write.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from eidolon_agent.config.settings import SqliteSettings
from eidolon_agent.domain.personas.types import PersonaEvolutionResult
from eidolon_agent.infra.persistence import (
    SqlPersonaInstanceStore,
    create_engine,
    create_session_factory,
    ensure_schema,
)

pytestmark = pytest.mark.functional


@pytest.fixture
async def sql_store():
    eng = create_engine(SqliteSettings(path=":memory:"))
    await ensure_schema(eng)
    sf = create_session_factory(eng)
    yield SqlPersonaInstanceStore(sf)
    await eng.dispose()


async def test_create_then_load_round_trips(sql_store, canonical_template_registry):
    template = canonical_template_registry.get("caretaker_jiezhi")
    created = await sql_store.create_from_template(
        template=template, tenant_id="t", user_id="alice", instance_id="i1"
    )
    loaded = await sql_store.load("t", "alice", "i1")
    assert loaded == created
    assert loaded.overlay_version == 1


async def test_save_overrides_existing(sql_store, canonical_template_registry):
    template = canonical_template_registry.get("caretaker_jiezhi")
    inst = await sql_store.create_from_template(
        template=template, tenant_id="t", user_id="alice", instance_id="i1"
    )
    bumped = inst.model_copy(
        update={"overlay_version": inst.overlay_version + 1,
                "updated_at": datetime.now(timezone.utc)}
    )
    await sql_store.save(bumped)
    fresh = await sql_store.load("t", "alice", "i1")
    assert fresh.overlay_version == 2


async def test_list_all_returns_every_tenant(sql_store, canonical_template_registry):
    template = canonical_template_registry.get("caretaker_jiezhi")
    await sql_store.create_from_template(
        template=template, tenant_id="t-a", user_id="alice", instance_id="i1"
    )
    await sql_store.create_from_template(
        template=template, tenant_id="t-b", user_id="bob", instance_id="i2"
    )
    rows = await sql_store.list_all()
    assert {r.instance_id for r in rows} == {"i1", "i2"}


async def test_delete_removes_row(sql_store, canonical_template_registry):
    template = canonical_template_registry.get("caretaker_jiezhi")
    await sql_store.create_from_template(
        template=template, tenant_id="t", user_id="alice", instance_id="i1"
    )
    await sql_store.delete("t", "alice", "i1")
    assert await sql_store.exists("t", "alice", "i1") is False


async def test_save_with_history_writes_both_rows(
    sql_store, canonical_template_registry
):
    """The single-TX evolution path: instance row + evolution_history row."""
    from sqlalchemy import select

    from eidolon_agent.infra.persistence.models import EvolutionHistoryRow

    template = canonical_template_registry.get("caretaker_jiezhi")
    inst = await sql_store.create_from_template(
        template=template, tenant_id="t", user_id="alice", instance_id="i1"
    )
    bumped = inst.model_copy(update={"overlay_version": 2})
    await sql_store.save_with_history(
        bumped,
        PersonaEvolutionResult(
            instance_id=inst.instance_id, applied=True, rationale="test"
        ),
    )
    # Instance bumped
    fresh = await sql_store.load("t", "alice", "i1")
    assert fresh.overlay_version == 2
    # History row appended
    async with sql_store._session_factory() as session:  # type: ignore[attr-defined]
        rows = (
            await session.execute(
                select(EvolutionHistoryRow).where(
                    EvolutionHistoryRow.instance_id == "i1"
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].rationale == "test"
