"""End-to-end tests for the custom-template CRUD surface (Phase 29.D).

These spin up a real Eidolon Data SQLite DB, a real registry (builtin yaml
templates from ``settings.persona.templates_dir`` + the custom preset store),
and the FastAPI app — so we cover:

  - Eidolon Data schema shape is correct
  - HTTP request/response wiring
  - Refcount-check on DELETE (uses a real ``persona_genomes`` row)
  - Cache refresh: writes propagate to the registry so subsequent
    ``GET /personas/templates`` shows the change
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from eidolon_data import DataSettings, DataStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eidolon_agent.app.admin.routers import personas as personas_router
from eidolon_agent.app.admin.routers import templates as templates_router
from eidolon_agent.domain.personas.registry import PersonaTemplateRegistry
from eidolon_agent.infra.persistence.eidolon_data_persona import (
    EidolonDataCustomTemplateStore,
    EidolonDataPersonaInstanceStore,
)

pytestmark = pytest.mark.functional


# A minimum valid PersonaTemplate yaml — used for create/fork tests. Built
# from the canonical types so a schema change here surfaces the test, not
# a production runtime error.
_VALID_YAML = """\
metadata:
  template_id: my_custom_v1
  template_revision: 1
  name: My Custom
  archetype: caretaker
  description: A test template.
identity_core:
  base_pronouns: "她"
behavioral_knobs: {}
style_compiler:
  base_instructions: []
  knob_mappings: {}
memory_adapter:
  retrieved_fact_handling:
    recency_bias: 0.5
    emotion_resonance: 0.5
"""


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[dict]:
    """Real Eidolon Data SQLite + registry pointed at a tmp templates_dir."""
    data_store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await data_store.init_schema()
    store = EidolonDataCustomTemplateStore(data_store)

    # Builtin templates dir with one minimal template so we can test
    # "cannot create custom with builtin id" + "fork builtin → custom".
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    builtin_yaml = _VALID_YAML.replace("my_custom_v1", "builtin_one")
    (templates_dir / "builtin_one.yaml").write_text(builtin_yaml, encoding="utf-8")

    reg = PersonaTemplateRegistry(templates_dir, custom_source=store)
    await reg.load_all()

    # Tiny app — just the two persona-related routers
    app = FastAPI()
    app.state.custom_template_store = store
    app.state.persona_template_registry = reg
    # personas router needs a personas_service for some endpoints, but
    # the templates router doesn't touch it — leave None and only call
    # /personas/templates/* routes from the templates module here.
    app.state.personas_service = None
    app.include_router(personas_router.router, prefix="/api/admin")
    app.include_router(templates_router.router, prefix="/api/admin")

    yield {
        "client": TestClient(app),
        "data_store": data_store,
        "store": store,
        "registry": reg,
    }
    await data_store.close()


# ---- create ----------------------------------------------------------------


def test_create_custom_template_201(env) -> None:
    client = env["client"]
    r = client.post(
        "/api/admin/personas/templates",
        json={
            "template_id": "my_custom_v1",
            "display_name": "My Custom",
            "yaml_body": _VALID_YAML,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["template_id"] == "my_custom_v1"
    assert body["revision"] == 1
    # Registry cache should have refreshed — list should include it
    reg: PersonaTemplateRegistry = env["registry"]
    ids = {s.template_id for s in reg.list_templates()}
    assert "my_custom_v1" in ids


def test_create_with_builtin_id_returns_409(env) -> None:
    """A template_id that matches a builtin must NOT be createable.
    Forking is the supported path."""
    client = env["client"]
    r = client.post(
        "/api/admin/personas/templates",
        json={
            "template_id": "builtin_one",
            "display_name": "Hijack",
            "yaml_body": _VALID_YAML,
        },
    )
    assert r.status_code == 409
    assert "builtin" in r.json()["detail"]


def test_create_with_invalid_yaml_returns_422(env) -> None:
    """Schema validation runs at the edge before DB write."""
    client = env["client"]
    r = client.post(
        "/api/admin/personas/templates",
        json={
            "template_id": "broken",
            "display_name": "Broken",
            "yaml_body": "not: [valid: yaml",
        },
    )
    assert r.status_code == 422


def test_create_duplicate_returns_409(env) -> None:
    client = env["client"]
    payload = {
        "template_id": "dup",
        "display_name": "First",
        "yaml_body": _VALID_YAML.replace("my_custom_v1", "dup"),
    }
    assert client.post("/api/admin/personas/templates", json=payload).status_code == 201
    r2 = client.post(
        "/api/admin/personas/templates",
        json={**payload, "display_name": "Second"},
    )
    assert r2.status_code == 409


# ---- update ----------------------------------------------------------------


def test_update_bumps_revision_and_yaml(env) -> None:
    client = env["client"]
    # Create
    client.post(
        "/api/admin/personas/templates",
        json={
            "template_id": "upd_test",
            "display_name": "v1",
            "yaml_body": _VALID_YAML.replace("my_custom_v1", "upd_test"),
        },
    )
    # Update yaml_body
    new_yaml = _VALID_YAML.replace("my_custom_v1", "upd_test").replace(
        "template_revision: 1", "template_revision: 2"
    )
    r = client.put(
        "/api/admin/personas/templates/upd_test",
        json={"display_name": "v2", "yaml_body": new_yaml},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["display_name"] == "v2"
    assert body["revision"] == 2  # bumped


def test_update_builtin_returns_409(env) -> None:
    client = env["client"]
    r = client.put(
        "/api/admin/personas/templates/builtin_one",
        json={"display_name": "Hijack"},
    )
    assert r.status_code == 409


def test_update_missing_returns_404(env) -> None:
    client = env["client"]
    r = client.put(
        "/api/admin/personas/templates/ghost",
        json={"display_name": "x"},
    )
    assert r.status_code == 404


# ---- delete (refcount check) ----------------------------------------------


def test_delete_removes_template(env) -> None:
    client = env["client"]
    client.post(
        "/api/admin/personas/templates",
        json={
            "template_id": "to_delete",
            "display_name": "Doomed",
            "yaml_body": _VALID_YAML.replace("my_custom_v1", "to_delete"),
        },
    )
    r = client.delete("/api/admin/personas/templates/to_delete")
    assert r.status_code == 204
    # Subsequent fetch via raw-custom returns 404
    r2 = client.get("/api/admin/personas/templates/to_delete/raw-custom")
    assert r2.status_code == 404


def test_delete_builtin_returns_409(env) -> None:
    client = env["client"]
    r = client.delete("/api/admin/personas/templates/builtin_one")
    assert r.status_code == 409
    assert "builtin" in r.json()["detail"]


def test_delete_missing_returns_404(env) -> None:
    client = env["client"]
    r = client.delete("/api/admin/personas/templates/ghost")
    assert r.status_code == 404


async def test_delete_refuses_when_persona_genomes_reference_it(env) -> None:
    """A preset referenced by a genome cannot be deleted in-place."""
    client = env["client"]

    # 1. Create the custom template
    client.post(
        "/api/admin/personas/templates",
        json={
            "template_id": "in_use",
            "display_name": "In Use",
            "yaml_body": _VALID_YAML.replace("my_custom_v1", "in_use"),
        },
    )

    # 2. Save a persona genome whose source_json records the generating template.
    instance_store = EidolonDataPersonaInstanceStore(env["data_store"])
    await instance_store.create_from_template(
        template=env["registry"].get("in_use"),
        tenant_id="default",
        user_id="alice",
        instance_id="inst_x",
    )

    # 3. DELETE the template — should refuse with 409
    r = client.delete("/api/admin/personas/templates/in_use")
    assert r.status_code == 409
    assert "in use" in r.json()["detail"]
    assert "1 persona" in r.json()["detail"]  # the count surfaces


# ---- fork ------------------------------------------------------------------


def test_fork_builtin_creates_custom_copy(env) -> None:
    client = env["client"]
    r = client.post(
        "/api/admin/personas/templates/builtin_one/fork",
        json={
            "new_template_id": "builtin_one_forked",
            "new_display_name": "Forked",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["template_id"] == "builtin_one_forked"
    # Source archetype preserved
    assert body["archetype"] == "caretaker"
    # Registry now lists both source and fork
    ids = {s.template_id for s in env["registry"].list_templates()}
    assert "builtin_one" in ids and "builtin_one_forked" in ids


def test_fork_with_builtin_target_id_returns_409(env) -> None:
    """Can't fork ONTO a builtin id — would clash with the read-only set."""
    client = env["client"]
    r = client.post(
        "/api/admin/personas/templates/builtin_one/fork",
        json={
            "new_template_id": "builtin_one",  # same as source = collision
            "new_display_name": "x",
        },
    )
    assert r.status_code == 409


def test_fork_missing_source_returns_404(env) -> None:
    client = env["client"]
    r = client.post(
        "/api/admin/personas/templates/ghost/fork",
        json={
            "new_template_id": "new",
            "new_display_name": "x",
        },
    )
    assert r.status_code == 404


# ---- raw-custom GET --------------------------------------------------------


def test_get_raw_custom_returns_yaml(env) -> None:
    client = env["client"]
    yaml_body = _VALID_YAML.replace("my_custom_v1", "raw_test")
    client.post(
        "/api/admin/personas/templates",
        json={
            "template_id": "raw_test",
            "display_name": "Raw",
            "yaml_body": yaml_body,
        },
    )
    r = client.get("/api/admin/personas/templates/raw_test/raw-custom")
    assert r.status_code == 200
    # Round-trip is verbatim
    assert r.text == yaml_body


def test_get_raw_custom_for_builtin_returns_404(env) -> None:
    """Builtin ids are NOT served by raw-custom — admin should hit the
    existing /raw endpoint for those. This 404 prevents the UI from
    accidentally letting the operator "edit" a builtin's yaml."""
    client = env["client"]
    r = client.get("/api/admin/personas/templates/builtin_one/raw-custom")
    assert r.status_code == 404
