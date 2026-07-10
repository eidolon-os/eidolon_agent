from __future__ import annotations

import pytest

from eidolon_agent.app.benchmark.product_acceptance import (
    ProductAcceptanceUnavailable,
    run_product_acceptance_profile,
)

pytestmark = pytest.mark.integration


async def test_product_acceptance_profile_runs_admin_onboarding_and_agent_grpc(tmp_path) -> None:
    try:
        result = await run_product_acceptance_profile(work_dir=tmp_path)
    except ProductAcceptanceUnavailable as exc:
        pytest.skip(str(exc))

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
        "companion.created",
        "persona.genome.committed",
        "memory_realm.created",
        "companion.workspace.initialized",
        "device.web_body.provisioned",
    }.issubset(set(result.event_types))
    assert result.cleanup_counts["deleted"] is True
    assert result.cleanup_counts["companions"] == 1
    assert result.cleanup_counts["persona_genomes"] == 1
    assert result.cleanup_counts["memory_realms"] == 1
    assert result.cleanup_counts["conversations"] >= 1
    assert result.cleanup_counts["turns"] >= 1
