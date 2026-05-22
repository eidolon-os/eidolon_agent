from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from eidolon_agent.app.admin import build_admin_app
from eidolon_agent.config.settings import Settings

pytestmark = pytest.mark.integration

@pytest.mark.asyncio
async def test_admin_personas_external_interface(
    personas_service,
    canonical_template_registry,
    persona_instance_store,
):
    app = build_admin_app(
        settings=Settings(),
        agent_registry=object(),
        pairing=object(),
        personas_service=personas_service,
    )
    client = TestClient(app)

    listed = client.get("/api/admin/personas/templates")
    assert listed.status_code == 200
    assert listed.json()[0]["template_id"] == "caretaker_jiezhi"

    created = client.post(
        "/api/admin/personas/instances",
        json={
            "tenant_id": "t",
            "user_id": "u",
            "instance_id": "i-admin",
            "template_id": "caretaker_jiezhi",
        },
    )
    assert created.status_code == 200

    preview = client.post(
        "/api/admin/personas/instances/t/u/i-admin/compile-preview",
        json={"user_text": "你好"},
    )
    assert preview.status_code == 200
    assert "你是「解之」" in preview.json()["system_prompt"]

    mock = client.post(
        "/api/admin/personas/instances/t/u/i-admin/mock-memory-trigger",
        json={
            "user_text": "又被老板骂了",
            "memory_hits": [
                {
                    "id": "h1",
                    "content": "用户提到老板是压力源",
                    "kind": "fact",
                    "metadata": {"relation_type": "user_stressors"},
                }
            ],
            "apply": False,
        },
    )
    assert mock.status_code == 200
    assert mock.json()["evolution"]["applied"] is False
