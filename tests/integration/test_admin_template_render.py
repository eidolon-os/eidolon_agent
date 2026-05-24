"""End-to-end test for POST /api/admin/personas/templates/{id}/render.

The route is the only contract eidolon_admin depends on for the device-
binding flow. These tests assert the wire shape and the contents of the
rendered markdown using a real PersonaTemplateRegistry loaded from the
project's actual template directory — there is no mock between the HTTP
boundary and the renderer.

Why integration-style: the renderer is pure but the route + Pydantic +
PersonasService composition is what admin actually calls, and the
template files (real YAML on disk) are the lookup target. Mocking the
service would only test the router scaffolding; running it against the
real service catches a class of integration bugs unit tests cannot
(template schema drift, route mounting, etc.).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from eidolon_agent.app.admin import build_admin_app
from eidolon_agent.config.settings import Settings

pytestmark = pytest.mark.integration


def _client(personas_service) -> TestClient:
    """Standalone admin app with only the personas surface — no agent
    registry / pairing wiring needed for the render endpoint."""
    app = build_admin_app(
        settings=Settings(),
        agent_registry=object(),
        pairing=object(),
        personas_service=personas_service,
    )
    return TestClient(app)


@pytest.mark.asyncio
async def test_render_endpoint_returns_markdown_and_revision_for_known_template(
    personas_service,
) -> None:
    """Happy path: an existing template renders into the documented envelope."""
    client = _client(personas_service)
    resp = client.post("/api/admin/personas/templates/caretaker_jiezhi/render")
    assert resp.status_code == 200
    body = resp.json()
    assert body["template_id"] == "caretaker_jiezhi"
    assert isinstance(body["template_revision"], int)
    assert body["template_revision"] >= 1
    # Sanity: markdown is non-empty and contains characteristic sections.
    md = body["markdown"]
    assert isinstance(md, str) and md.strip()
    assert "# 解之" in md  # template's display name appears as H1
    assert "## 身份核心" in md  # identity_core section
    assert "## 行为旋钮" in md  # knobs section
    assert "## 表达风格" in md  # active style instructions


@pytest.mark.asyncio
async def test_render_endpoint_does_not_create_instance(
    personas_service,
    persona_instance_store,
) -> None:
    """The renderer is documented as a *pure read* — it must NEVER side-
    effect into the instance store. Catching a regression here protects
    the admin orchestrator's mental model: render is idempotent + side-
    effect-free + safe to retry."""
    client = _client(personas_service)
    # Multiple calls — none should populate the instance store.
    for _ in range(3):
        resp = client.post("/api/admin/personas/templates/caretaker_jiezhi/render")
        assert resp.status_code == 200
    # PersonaInstanceStore should still be empty.
    listed = await persona_instance_store.list_all()
    assert listed == []


@pytest.mark.asyncio
async def test_render_endpoint_404s_unknown_template(personas_service) -> None:
    """Missing template id → 404 with a useful detail string."""
    client = _client(personas_service)
    resp = client.post("/api/admin/personas/templates/does_not_exist/render")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_render_endpoint_is_deterministic_across_calls(
    personas_service,
) -> None:
    """Same template → byte-identical markdown across calls.

    Admin relies on this being a function of the template alone — if it
    were dependent on time/random state, every reload would produce a
    different soul.md and history would be useless."""
    client = _client(personas_service)
    a = client.post("/api/admin/personas/templates/caretaker_jiezhi/render").json()["markdown"]
    b = client.post("/api/admin/personas/templates/caretaker_jiezhi/render").json()["markdown"]
    assert a == b
