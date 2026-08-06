from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from eidolon_data import DataSettings, DataStore

from eidolon_agent.app.benchmark import product_acceptance

pytestmark = pytest.mark.integration


async def test_product_acceptance_profile_runs_admin_onboarding_and_agent_grpc(tmp_path) -> None:
    try:
        result = await product_acceptance.run_product_acceptance_profile(work_dir=tmp_path)
    except product_acceptance.ProductAcceptanceUnavailable as exc:
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
            await store.owners.create(owner_id=owner_id, display_name="Partial Owner")
        finally:
            await store.close()
        raise RuntimeError("forced onboarding failure")

    monkeypatch.setattr(
        product_acceptance,
        "_initialize_via_admin_onboarding_api",
        fail_after_partial_owner,
    )

    with pytest.raises(RuntimeError, match="forced onboarding failure"):
        await product_acceptance.run_product_acceptance_profile(work_dir=tmp_path)

    store = DataStore.open(
        DataSettings(sqlite_path=str(tmp_path / "eidolon-system.sqlite3"))
    )
    try:
        await store.init_schema()
        assert await store.owners.get("owner_acceptance") is None
    finally:
        await store.close()


def test_product_acceptance_admin_import_does_not_leave_sibling_path(monkeypatch) -> None:
    admin_server = (
        product_acceptance.Path(product_acceptance.__file__).resolve().parents[4]
        / "eidolon_admin"
        / "server"
    )
    admin_path = str(admin_server)
    if admin_path in sys.path:
        pytest.skip("admin server path already present in this test process")

    calls: list[bool] = []

    def fake_import_module(module_name: str):
        assert module_name == "eidolon_admin_server.app.onboarding.router"
        calls.append(admin_path in sys.path)
        if len(calls) == 1:
            raise ImportError("not importable without sibling path")
        return SimpleNamespace(router=object())

    monkeypatch.setattr(
        product_acceptance.importlib,
        "import_module",
        fake_import_module,
    )

    module = product_acceptance._import_admin_module(
        "eidolon_admin_server.app.onboarding.router"
    )

    assert module.router is not None
    assert calls == [False, True]
    assert admin_path not in sys.path
