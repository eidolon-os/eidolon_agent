"""Admin /api/admin/pairing/codes issues owner/companion pairing codes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from eidolon_data import DataSettings, DataStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eidolon_agent.app.admin.routers import pairing as pairing_router
from eidolon_agent.app.transport.pairing import PairingCoordinator
from eidolon_agent.infra.memory.discovery import (
    DiscoveryResponse,
    MemoryNatsRoute,
    MemoryRoute,
    MemoryRoutingTable,
)

pytestmark = pytest.mark.functional


def _routes(*routes: MemoryRoute) -> MemoryRoutingTable:
    return MemoryRoutingTable(
        nats=MemoryNatsRoute(
            url="nats://x",
            stream="",
            turn_subject_template="t",
            cmd_subject_template="c",
        ),
        routes={r.memory_space_id: r for r in routes},
    )


async def _store(tmp_path) -> DataStore:
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await store.init_schema()
    await store.owner_service.create_owner(owner_id="alice", display_name="Alice")
    await store.companion_workspace.initialize_workspace(
        owner_id="alice",
        companion_id="companion-a",
        genome_id="genome-a",
        realm_id="realm-a",
    )
    return store


def _client(store: DataStore, *, routes: MemoryRoutingTable | None = None, refresher=None) -> TestClient:
    app = FastAPI()
    app.include_router(pairing_router.router, prefix="/api/admin")
    app.state.pairing = PairingCoordinator(jwt_secret="test-secret-not-for-prod-x" * 2)
    app.state.data_store = store
    if routes is not None:
        app.state.memory_routes = routes
    app.state.memory_discovery_refresher = refresher
    return TestClient(app)


def test_issue_code_returns_8_char_alphanumeric(tmp_path) -> None:
    store = asyncio.run(_store(tmp_path))
    try:
        with _client(store) as client:
            r = client.post(
                "/api/admin/pairing/codes",
                json={"owner_id": "alice", "companion_id": "companion-a"},
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body["code"]) == 8
        assert body["pair_url"].startswith("eidolon://pair?code=")
        assert body["pair_url"].endswith(body["code"])
    finally:
        asyncio.run(store.close())


def test_issue_code_reports_memory_readiness_when_route_exists(tmp_path) -> None:
    store = asyncio.run(_store(tmp_path))
    try:
        with _client(
            store,
            routes=_routes(
                MemoryRoute(
                    memory_space_id="realm-a",
                    mcp_url="http://127.0.0.1:8031/mcp",
                )
            ),
        ) as client:
            r = client.post(
                "/api/admin/pairing/codes",
                json={"owner_id": "alice", "companion_id": "companion-a"},
            )

        assert r.status_code == 200
        body = r.json()
        assert body["memory"] == {
            "ready": True,
            "owner_id": "alice",
            "companion_id": "companion-a",
            "memory_space_id": "realm-a",
            "memory_realm_id": "realm-a",
            "reason": None,
            "mcp_http_url": "http://127.0.0.1:8031/mcp",
        }
    finally:
        asyncio.run(store.close())


async def test_memory_readiness_uses_companion_default_realm(tmp_path) -> None:
    store = await _store(tmp_path)
    routes = _routes(
        MemoryRoute(
            memory_space_id="realm-a",
            mcp_url="http://127.0.0.1:8031/mcp",
        )
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                data_store=store,
                memory_routes=routes,
                memory_discovery_refresher=None,
            )
        )
    )
    try:
        identity = await pairing_router._resolve_pairing_identity(
            owner_id="alice",
            companion_id="companion-a",
            request=request,
        )
        readiness = await pairing_router._ensure_memory_provisioned(
            identity=identity,
            request=request,
        )
    finally:
        await store.close()

    assert readiness is not None
    assert readiness.memory_space_id == "realm-a"
    assert readiness.memory_realm_id == "realm-a"


def test_issue_code_rejects_unprovisioned_memory_realm(tmp_path) -> None:
    store = asyncio.run(_store(tmp_path))
    try:
        with _client(store, routes=_routes()) as client:
            r = client.post(
                "/api/admin/pairing/codes",
                json={"owner_id": "alice", "companion_id": "companion-a"},
            )

        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "memory_user_not_provisioned"
        assert detail["owner_id"] == "alice"
        assert detail["companion_id"] == "companion-a"
        assert detail["reason"] == "no_memory_route"
    finally:
        asyncio.run(store.close())


def test_issue_code_refreshes_discovery_before_rejecting(tmp_path) -> None:
    store = asyncio.run(_store(tmp_path))
    routes = _routes()

    class _Refresher:
        async def refresh_once(self) -> bool:
            await routes.replace_from_discovery(
                DiscoveryResponse.model_validate(
                    {
                        "nats": {"url": "nats://x"},
                        "users": [
                            {
                                "memory_space_id": "realm-a",
                                "owner_id": "alice",
                                "companion_id": "companion-a",
                                "mcp_http_url": "http://127.0.0.1:8031/mcp",
                                "enabled": True,
                                "agent_reachable": True,
                            }
                        ],
                    }
                )
            )
            return True

    try:
        with _client(store, routes=routes, refresher=_Refresher()) as client:
            r = client.post(
                "/api/admin/pairing/codes",
                json={"owner_id": "alice", "companion_id": "companion-a"},
            )

        assert r.status_code == 200
        assert r.json()["memory"]["ready"] is True
    finally:
        asyncio.run(store.close())


def test_issue_code_rejects_unknown_companion(tmp_path) -> None:
    store = asyncio.run(_store(tmp_path))
    try:
        with _client(store) as client:
            r = client.post(
                "/api/admin/pairing/codes",
                json={"owner_id": "alice", "companion_id": "missing"},
            )
        assert r.status_code == 404
    finally:
        asyncio.run(store.close())


def test_qr_endpoint_returns_png(tmp_path) -> None:
    store = asyncio.run(_store(tmp_path))
    try:
        with _client(store) as client:
            issued = client.post(
                "/api/admin/pairing/codes",
                json={"owner_id": "alice", "companion_id": "companion-a"},
            ).json()
            r = client.get(f"/api/admin/pairing/codes/{issued['code']}.png")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    finally:
        asyncio.run(store.close())
