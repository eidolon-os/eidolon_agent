from __future__ import annotations

import pytest
from eidolon_data import DataSettings, DataStore

from eidolon_agent.app.benchmark import product_acceptance

pytestmark = pytest.mark.integration


async def test_product_acceptance_profile_runs_system_data_and_agent_grpc(tmp_path) -> None:
    result = await product_acceptance.run_product_acceptance_profile(work_dir=tmp_path)

    assert result.passed is True
    assert result.mode == "deterministic"
    assert "DONE" in result.chat_event_kinds
    assert result.turn_metadata == {
        "owner_id": result.owner_id,
        "companion_id": result.companion_id,
        "memory_realm_id": result.memory_realm_id,
        "genome_id": result.genome_id,
        "genome_hash": result.genome_hash,
    }
    assert {
        "owner.created",
        "companion.workspace.initialized",
    }.issubset(set(result.event_types))
    assert result.cleanup_counts["deleted"] is True
    assert result.cleanup_counts["companions"] == 1
    assert result.cleanup_counts["persona_genomes"] == 1
    assert result.cleanup_counts["memory_realms"] == 1
    assert result.cleanup_counts["conversations"] >= 1
    assert result.cleanup_counts["turns"] >= 1


async def test_product_acceptance_profile_cleans_partial_onboarding_failure(
    tmp_path,
    monkeypatch,
) -> None:
    async def fail_after_partial_owner(
        *,
        sqlite_path,
        owner_id: str,
        companion_id: str,
    ) -> dict[str, str]:
        del companion_id
        store = DataStore.open(DataSettings(sqlite_path=str(sqlite_path)))
        try:
            await store.init_schema()
            await store.owner_commands.create_owner(
                owner_id=owner_id,
                display_name="Partial Owner",
            )
        finally:
            await store.close()
        raise RuntimeError("forced onboarding failure")

    monkeypatch.setattr(
        product_acceptance,
        "_initialize_local_system_data",
        fail_after_partial_owner,
    )

    with pytest.raises(RuntimeError, match="forced onboarding failure"):
        await product_acceptance.run_product_acceptance_profile(work_dir=tmp_path)

    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon-system.sqlite3")))
    try:
        await store.init_schema()
        assert await store.owners.get("owner_acceptance") is None
    finally:
        await store.close()
