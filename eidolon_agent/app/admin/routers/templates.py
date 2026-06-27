"""Admin — custom persona template CRUD (Phase 29.D).

Routes are *adjacent* to ``personas.py``'s ``/personas/templates``
read-only endpoints (list / detail / raw / render) — those already
exist and serve the builtin + custom merged view. This file adds the
write surface that admin's Templates module needs:

  POST   /personas/templates                  create custom
  PUT    /personas/templates/{id}             update custom
  DELETE /personas/templates/{id}             delete custom (refcount-checked)
  POST   /personas/templates/{id}/fork        fork builtin → custom

Built-in templates are deployment artifacts (YAML files); they're not
mutable through this surface. Attempting to write to a builtin id is
refused with 409. Forking is the supported path to "derive from a
builtin".

State coupling:
    Writes go through the wired custom template store, then trigger
    ``registry.refresh_custom()`` so the in-memory cache (consulted by
    rendering / turn compile) stays consistent. Skipping the refresh
    would mean an operator's edit is persisted but the running agent
    keeps rendering from the old version until restart.
"""

from __future__ import annotations

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from eidolon_agent.domain.personas.types import PersonaTemplate

router = APIRouter()


# ---- request bodies --------------------------------------------------------


class CreateCustomTemplateRequest(BaseModel):
    template_id: str = Field(..., min_length=1, max_length=128)
    tenant_id: str = Field("default", min_length=1, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=255)
    archetype: str = Field("custom", min_length=1, max_length=64)
    # The full YAML document as text. Validated server-side by attempting
    # to parse via PersonaTemplate — see ``_validate_yaml`` below.
    yaml_body: str = Field(..., min_length=1)


class UpdateCustomTemplateRequest(BaseModel):
    """Partial update. None on a field means "leave unchanged"."""

    display_name: str | None = Field(None, min_length=1, max_length=255)
    yaml_body: str | None = Field(None, min_length=1)


class ForkTemplateRequest(BaseModel):
    new_template_id: str = Field(..., min_length=1, max_length=128)
    target_tenant_id: str = Field("default", min_length=1, max_length=64)
    new_display_name: str = Field(..., min_length=1, max_length=255)


# ---- helpers ---------------------------------------------------------------


def _store(request: Request):
    store = request.app.state.custom_template_store
    if store is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "custom template store unavailable — agent bootstrap did "
                "not wire it (check eidolon_data initialization)"
            ),
        )
    return store


def _registry(request: Request):
    reg = request.app.state.persona_template_registry
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail="persona template registry unavailable",
        )
    return reg


def _validate_yaml_renders(yaml_body: str) -> None:
    """Parse + schema-validate yaml. Raises ``HTTPException(422)`` on bad
    yaml or schema mismatch. Done at the HTTP edge so the operator gets
    a clear validation error BEFORE the row is persisted."""
    try:
        raw = yaml.safe_load(yaml_body)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=f"YAML parse error: {exc}") from exc
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=422, detail="template root must be a mapping"
        )
    try:
        PersonaTemplate(**raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"template schema: {exc}") from exc


# ---- endpoints -------------------------------------------------------------


@router.post("/personas/templates", status_code=201)
async def create_custom_template(
    body: CreateCustomTemplateRequest, request: Request
):
    """Create a custom template. ``template_id`` must NOT collide with a
    builtin (operator should ``fork`` instead) and must be unique among
    customs.
    """
    reg = _registry(request)
    if body.template_id in reg.builtin_ids():
        raise HTTPException(
            status_code=409,
            detail=(
                f"template_id {body.template_id!r} is a builtin; "
                "fork the builtin under a new id instead of overwriting"
            ),
        )
    _validate_yaml_renders(body.yaml_body)

    store = _store(request)
    # Import error types lazily to keep the router module light.
    from eidolon_agent.infra.persistence import (
        CustomTemplateAlreadyExists,
    )
    try:
        view = await store.create(
            template_id=body.template_id,
            tenant_id=body.tenant_id,
            display_name=body.display_name,
            archetype=body.archetype,
            yaml_body=body.yaml_body,
        )
    except CustomTemplateAlreadyExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await reg.refresh_custom()
    return view.to_dict()


@router.put("/personas/templates/{template_id}")
async def update_custom_template(
    template_id: str, body: UpdateCustomTemplateRequest, request: Request
):
    """Update a custom template's display_name and/or yaml_body. Revision
    bumps on every PUT (even if no fields changed) — kept that way so the
    audit trail counts every "operator clicked save" as an event."""
    reg = _registry(request)
    if template_id in reg.builtin_ids():
        raise HTTPException(
            status_code=409,
            detail=(
                f"template {template_id!r} is a builtin and cannot be edited "
                "in place; fork it first to create an editable copy"
            ),
        )
    if body.yaml_body is not None:
        _validate_yaml_renders(body.yaml_body)

    store = _store(request)
    from eidolon_agent.infra.persistence import (
        CustomTemplateNotFound,
    )
    try:
        view = await store.update(
            template_id,
            display_name=body.display_name,
            yaml_body=body.yaml_body,
        )
    except CustomTemplateNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await reg.refresh_custom()
    return view.to_dict()


@router.delete("/personas/templates/{template_id}", status_code=204)
async def delete_custom_template(template_id: str, request: Request) -> None:
    """Delete a custom template after a refcount check.

    Refuses if any ``persona_instance`` was rendered from this template
    (would orphan running agents). Operator must delete or migrate those
    instances first.
    """
    reg = _registry(request)
    if template_id in reg.builtin_ids():
        raise HTTPException(
            status_code=409,
            detail=(
                f"template {template_id!r} is a builtin and cannot be deleted; "
                "remove the yaml file from templates_dir and redeploy"
            ),
        )

    store = _store(request)
    # Refcount check FIRST so a "in use" error 409 takes precedence over
    # the "not found" 404 — operators are more confused by orphan agents
    # than by missing templates.
    in_use = await store.count_referring_instances(template_id)
    if in_use > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"template {template_id!r} is in use by {in_use} persona "
                "instance(s); delete or migrate those first"
            ),
        )

    from eidolon_agent.infra.persistence import (
        CustomTemplateNotFound,
    )
    try:
        await store.delete(template_id)
    except CustomTemplateNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await reg.refresh_custom()
    # 204 — no body


@router.post("/personas/templates/{template_id}/fork", status_code=201)
async def fork_template(
    template_id: str, body: ForkTemplateRequest, request: Request
):
    """Copy a builtin OR custom template under a new id. Forking is the
    only way to derive an editable copy from a builtin.

    The forked copy:
      - takes ``new_template_id`` and ``new_display_name`` from the body
      - preserves the source yaml verbatim (the operator edits via PUT
        afterwards if they want to diverge)
      - preserves the source ``archetype`` so the fork is "the same kind
        of persona, just a different ID"
    """
    reg = _registry(request)
    # Source has to exist (in either source).
    try:
        source_yaml = reg.raw_yaml(template_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404, detail=f"source template not found: {exc}"
        ) from exc
    # The fork's target id must not collide with a builtin.
    if body.new_template_id in reg.builtin_ids():
        raise HTTPException(
            status_code=409,
            detail=(
                f"new_template_id {body.new_template_id!r} is a builtin id; "
                "pick a different id for the fork"
            ),
        )
    # Derive archetype from the source's parsed metadata. Use try/except
    # so a broken source-yaml doesn't crash the fork (unlikely for
    # builtin, but defensive).
    try:
        source_template = reg.get(template_id)
        source_archetype = source_template.metadata.archetype or "custom"
    except Exception:
        source_archetype = "custom"

    store = _store(request)
    from eidolon_agent.infra.persistence import (
        CustomTemplateAlreadyExists,
    )
    try:
        view = await store.create(
            template_id=body.new_template_id,
            tenant_id=body.target_tenant_id,
            display_name=body.new_display_name,
            archetype=source_archetype,
            yaml_body=source_yaml,
        )
    except CustomTemplateAlreadyExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await reg.refresh_custom()
    return view.to_dict()


@router.get(
    "/personas/templates/{template_id}/raw-custom",
    response_class=PlainTextResponse,
)
async def get_raw_custom_template(template_id: str, request: Request):
    """Return raw YAML if this id is a custom template.

    The existing ``GET /personas/templates/{id}/raw`` endpoint already
    serves both builtin and custom (since the registry merges); this one
    is the explicit-custom-only variant the admin Templates UI uses when
    fetching the editor's initial yaml — guarantees an unambiguous 404
    if the operator accidentally targets a builtin id.
    """
    reg = _registry(request)
    if not reg.is_custom(template_id):
        raise HTTPException(
            status_code=404, detail=f"no custom template with id {template_id!r}"
        )
    return reg.raw_yaml(template_id)
