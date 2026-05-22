"""Admin /api/admin/agents router — start/list/stop via TestClient."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eidolon_agent.app.admin.routers import agents

pytestmark = pytest.mark.functional


class _StubRegistry:
    """Minimal AgentRegistry stand-in matching what the router calls."""

    def __init__(self) -> None:
        self._instances: dict[str, _StubInstance] = {}
        self._counter = 0

    async def start_instance(
        self,
        *,
        template_id: str,
        tenant_id: str,
        user_id: str,
        nickname_alias: str | None,
        explicit_instance_id: str | None,
    ):
        self._counter += 1
        iid = explicit_instance_id or f"inst-{self._counter}"
        if iid in self._instances:
            from eidolon_agent.core.errors import ConflictError

            raise ConflictError(f"already exists: {iid}")
        inst = _StubInstance(iid, template_id, tenant_id, user_id, nickname_alias)
        self._instances[iid] = inst
        return inst

    async def stop_instance(self, instance_id: str) -> None:
        self._instances.pop(instance_id, None)

    def list_instances(self) -> list:
        return list(self._instances.values())


class _StubInstance:
    def __init__(self, iid, tpl, ten, usr, alias) -> None:
        self.instance_id = iid
        self.template_id = tpl
        self.tenant_id = ten
        self.user_id = usr
        self.nickname_alias = alias
        self.status = "active"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(agents.router, prefix="/api/admin")
    app.state.agent_registry = _StubRegistry()
    return TestClient(app)


def test_start_agent_returns_201_with_instance_info(client: TestClient) -> None:
    r = client.post(
        "/api/admin/agents",
        json={"template_id": "tpl", "tenant_id": "t", "user_id": "alice"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["tenant_id"] == "t"
    assert body["user_id"] == "alice"
    assert body["status"] == "active"


def test_list_agents_returns_started_instances(client: TestClient) -> None:
    client.post(
        "/api/admin/agents",
        json={"template_id": "tpl", "tenant_id": "t", "user_id": "u1"},
    )
    client.post(
        "/api/admin/agents",
        json={"template_id": "tpl", "tenant_id": "t", "user_id": "u2"},
    )
    r = client.get("/api/admin/agents")
    assert r.status_code == 200
    users = sorted(i["user_id"] for i in r.json())
    assert users == ["u1", "u2"]


def test_start_agent_with_existing_id_returns_409(client: TestClient) -> None:
    body = {
        "template_id": "tpl",
        "tenant_id": "t",
        "user_id": "u",
        "instance_id": "fixed-id",
    }
    assert client.post("/api/admin/agents", json=body).status_code == 201
    r = client.post("/api/admin/agents", json=body)
    assert r.status_code == 409
    assert "fixed-id" in r.json()["detail"]


def test_stop_agent_returns_204(client: TestClient) -> None:
    started = client.post(
        "/api/admin/agents",
        json={"template_id": "tpl", "tenant_id": "t", "user_id": "u"},
    ).json()
    r = client.delete(f"/api/admin/agents/{started['instance_id']}")
    assert r.status_code == 204
    # Subsequent list should be empty.
    assert client.get("/api/admin/agents").json() == []
