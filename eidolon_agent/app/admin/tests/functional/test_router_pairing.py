"""Admin /api/admin/pairing/codes — issue code via TestClient."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from eidolon_data import DataSettings, DataStore
from eidolon_data.adapters.admin_registry import (
    EidolonDataAgentMetadataRepository,
    EidolonDataUserRepository,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from eidolon_sdk.biz.registry.models import AgentMetadataRecord, UserRegistryRecord

from eidolon_agent.app.admin.routers import pairing as pairing_router
from eidolon_agent.app.transport.pairing import PairingCoordinator
from eidolon_agent.infra.memory.discovery import (
    DiscoveryResponse,
    MemoryNatsRoute,
    MemoryRoute,
    MemoryRoutingTable,
)

pytestmark = pytest.mark.functional


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(pairing_router.router, prefix="/api/admin")
    app.state.pairing = PairingCoordinator(jwt_secret="test-secret-not-for-prod-x" * 2)
    return TestClient(app)


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


def _client_with_routes(routes: MemoryRoutingTable, refresher=None) -> TestClient:
    app = FastAPI()
    app.include_router(pairing_router.router, prefix="/api/admin")
    app.state.pairing = PairingCoordinator(jwt_secret="test-secret-not-for-prod-x" * 2)
    app.state.memory_routes = routes
    app.state.memory_discovery_refresher = refresher
    return TestClient(app)


def test_issue_code_returns_8_char_alphanumeric(client: TestClient) -> None:
    r = client.post(
        "/api/admin/pairing/codes",
        json={"tenant_id": "t", "user_id": "alice", "default_template_id": "tpl"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["code"]) == 8
    assert body["pair_url"].startswith("eidolon://pair?code=")
    assert body["pair_url"].endswith(body["code"])


def test_issue_code_reports_memory_readiness_when_route_exists() -> None:
    client = _client_with_routes(
        _routes(
            MemoryRoute(
                memory_space_id="t.alice.tpl",
                mcp_url="http://127.0.0.1:8031/mcp",
            )
        )
    )

    r = client.post(
        "/api/admin/pairing/codes",
        json={"tenant_id": "t", "user_id": "alice", "default_template_id": "tpl"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["memory"] == {
        "ready": True,
        "user_id": "alice",
        "memory_space_id": "t.alice.tpl",
        "reason": None,
        "mcp_http_url": "http://127.0.0.1:8031/mcp",
    }


async def test_memory_readiness_uses_active_agent_instance_when_data_store_exists(tmp_path) -> None:
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await store.init_schema()
    await EidolonDataUserRepository(store).put(
        UserRegistryRecord(
            user_id="alice",
            tenant_id="t",
            active_agent_id="agent-active",
            enabled=True,
        )
    )
    await EidolonDataAgentMetadataRepository(store).put(
        AgentMetadataRecord(
            agent_id="agent-active",
            tenant_id="t",
            user_id="alice",
            template_id="tpl",
        )
    )
    routes = _routes(
        MemoryRoute(
            memory_space_id="t.alice.agent-active",
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
        readiness = await pairing_router._ensure_memory_provisioned(
            tenant_id="t",
            user_id="alice",
            default_template_id="tpl",
            request=request,
        )
    finally:
        await store.close()

    assert readiness is not None
    assert readiness.memory_space_id == "t.alice.agent-active"


def test_issue_code_rejects_unprovisioned_memory_user() -> None:
    client = _client_with_routes(_routes())

    r = client.post(
        "/api/admin/pairing/codes",
        json={"tenant_id": "t", "user_id": "ghost", "default_template_id": "tpl"},
    )

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "memory_user_not_provisioned"
    assert detail["user_id"] == "ghost"
    assert detail["reason"] == "no_memory_route"


def test_issue_code_refreshes_discovery_before_rejecting() -> None:
    routes = _routes()

    class _Refresher:
        async def refresh_once(self) -> bool:
            await routes.replace_from_discovery(
                DiscoveryResponse.model_validate(
                    {
                        "nats": {"url": "nats://x"},
                        "users": [
                        {
                            "memory_space_id": "t.alice.tpl",
                            "tenant_id": "t",
                            "owner_user_id": "alice",
                            "companion_id": "tpl",
                            "mcp_http_url": "http://127.0.0.1:8031/mcp",
                            "enabled": True,
                                "agent_reachable": True,
                            }
                        ],
                    }
                )
            )
            return True

    client = _client_with_routes(routes, refresher=_Refresher())

    r = client.post(
        "/api/admin/pairing/codes",
        json={"tenant_id": "t", "user_id": "alice", "default_template_id": "tpl"},
    )

    assert r.status_code == 200
    assert r.json()["memory"]["ready"] is True


def test_issue_code_without_template_is_optional(client: TestClient) -> None:
    r = client.post(
        "/api/admin/pairing/codes",
        json={"tenant_id": "t", "user_id": "u"},
    )
    assert r.status_code == 200


def test_qr_endpoint_returns_png(client: TestClient) -> None:
    issued = client.post(
        "/api/admin/pairing/codes",
        json={"tenant_id": "t", "user_id": "u"},
    ).json()
    r = client.get(f"/api/admin/pairing/codes/{issued['code']}.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
