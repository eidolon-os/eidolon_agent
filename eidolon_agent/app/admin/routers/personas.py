"""Admin: persona templates and instances."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from eidolon_agent.core.errors import (
    ConflictError,
    EvolutionGuardError,
    NotFoundError,
    ValidationError,
)
from eidolon_agent.core.types.memory import MemoryHit, MemoryKind
from eidolon_agent.domain.personas import PersonaEvolutionEvent, render_template_markdown

router = APIRouter()


class CreateInstanceRequest(BaseModel):
    tenant_id: str
    user_id: str
    instance_id: str
    template_id: str


class CompilePreviewRequest(BaseModel):
    user_text: str = ""
    template_id: str | None = None
    realtime: dict | None = None
    memory_hits: list[dict] | None = None


class EvolveRequest(BaseModel):
    events: list[dict] = Field(default_factory=list)
    dry_run: bool = False
    template_id: str | None = None


class MockMemoryTriggerRequest(BaseModel):
    user_text: str = ""
    template_id: str | None = None
    memory_hits: list[dict] = Field(default_factory=list)
    apply: bool = False


class RollbackRequest(BaseModel):
    delta_id: str


class ReflectRequest(BaseModel):
    dry_run: bool = False
    auto_apply: bool = True
    limit: int = Field(default=50, ge=1, le=500)


class ProposalDecisionRequest(BaseModel):
    actor: str = "admin"
    reason: str | None = None


@router.get("/personas/templates")
async def list_templates(request: Request):
    service = _service(request)
    return [t.model_dump(mode="json") for t in await service.list_templates()]


@router.post("/personas/templates/reload")
async def reload_templates(request: Request):
    """Re-scan ``templates_dir`` and return the new template count."""
    service = _service(request)
    count = await service.reload_templates()
    return {"loaded": count}


@router.get("/personas/templates/{template_id}")
async def get_template(template_id: str, request: Request):
    service = _service(request)
    try:
        tpl = await service.get_template(template_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return tpl.model_dump(mode="json")


@router.get("/personas/templates/{template_id}/raw", response_class=PlainTextResponse)
async def get_template_raw(template_id: str, request: Request):
    """Return the original YAML source for a template — admin read-only view."""
    service = _service(request)
    try:
        return await service.get_template_raw(template_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@router.post("/personas/templates/{template_id}/render")
async def render_template(template_id: str, request: Request):
    """Render the template's initial state into a markdown soul document.

    This is the bind-time entry point used by ``eidolon_admin`` to materialize
    a per-device "soul.md" in NATS KV. Pure read: no instance is created,
    no SQL is touched, no NATS key is written from here. Admin owns the
    write-side; this endpoint is a stable, side-effect-free template →
    markdown conversion.

    Why it lives on the agent side (not in admin):
        Only the agent owns the canonical template schema (identity_core,
        knobs, style_compiler, etc.); reproducing render logic in admin
        would mean shipping that schema across two repos.
    """
    service = _service(request)
    try:
        tpl = await service.get_template(template_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    markdown = render_template_markdown(tpl)
    return {
        "markdown": markdown,
        "template_id": tpl.metadata.template_id,
        "template_revision": tpl.metadata.template_revision,
    }


@router.get("/personas/instances")
async def list_instances(request: Request):
    """List every persona instance across tenants/users.

    Used by the admin UI to render the global instance table; ordered by
    ``last_active_at DESC`` at the store level.
    """
    service = _service(request)
    rows = await service.list_instances()
    return [
        {
            "instance_id": r.instance_id,
            "tenant_id": r.tenant_id,
            "user_id": r.user_id,
            "template_id": r.origin_template_id,
            "overlay_version": r.overlay_version,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
        }
        for r in rows
    ]


@router.post("/personas/instances")
async def create_instance(body: CreateInstanceRequest, request: Request):
    service = _service(request)
    instance = await service.create_instance(
        tenant_id=body.tenant_id,
        user_id=body.user_id,
        instance_id=body.instance_id,
        template_id=body.template_id,
    )
    return instance.model_dump(mode="json")


@router.get("/personas/instances/{tenant_id}/{user_id}/{instance_id}")
async def get_instance(tenant_id: str, user_id: str, instance_id: str, request: Request):
    service = _service(request)
    try:
        instance = await service.get_instance(
            tenant_id=tenant_id,
            user_id=user_id,
            instance_id=instance_id,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return instance.model_dump(mode="json")


@router.get("/personas/instances/{tenant_id}/{user_id}/{instance_id}/snapshot")
async def get_instance_snapshot(tenant_id: str, user_id: str, instance_id: str, request: Request):
    """Current snapshot: instance + runtime state + prompt hint."""
    service = _service(request)
    try:
        snapshot = await service.get_snapshot(
            tenant_id=tenant_id,
            user_id=user_id,
            instance_id=instance_id,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return snapshot.model_dump(mode="json")


@router.get("/personas/instances/{tenant_id}/{user_id}/{instance_id}/evolution")
async def list_instance_evolution(
    tenant_id: str,
    user_id: str,
    instance_id: str,
    request: Request,
    limit: int = 50,
):
    """Recent applied evolution history for the instance (most-recent first)."""
    service = _service(request)
    history = await service.list_evolution_history(instance_id, limit=limit)
    return [r.model_dump(mode="json") for r in history]


@router.get("/personas/instances/{tenant_id}/{user_id}/{instance_id}/observations")
async def list_instance_observations(
    tenant_id: str,
    user_id: str,
    instance_id: str,
    request: Request,
    status: str | None = None,
    limit: int = 50,
):
    service = _service(request)
    observations = await service.list_observations(
        instance_id,
        status=status,
        limit=limit,
    )
    return [row.model_dump(mode="json") for row in observations]


@router.get("/personas/instances/{tenant_id}/{user_id}/{instance_id}/proposals")
async def list_instance_proposals(
    tenant_id: str,
    user_id: str,
    instance_id: str,
    request: Request,
    status: str | None = None,
    limit: int = 50,
):
    service = _service(request)
    proposals = await service.list_evolution_proposals(
        instance_id,
        status=status,
        limit=limit,
    )
    return [row.model_dump(mode="json") for row in proposals]


@router.post("/personas/instances/{tenant_id}/{user_id}/{instance_id}/reflect")
async def reflect_instance(
    tenant_id: str,
    user_id: str,
    instance_id: str,
    body: ReflectRequest,
    request: Request,
):
    service = _service(request)
    try:
        proposals = await service.run_reflection(
            tenant_id=tenant_id,
            user_id=user_id,
            instance_id=instance_id,
            dry_run=body.dry_run,
            auto_apply=body.auto_apply,
            limit=body.limit,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return [row.model_dump(mode="json") for row in proposals]


@router.get("/personas/evolution-proposals/{proposal_id}")
async def get_evolution_proposal(proposal_id: str, request: Request):
    service = _service(request)
    try:
        proposal = await service.get_evolution_proposal(proposal_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return proposal.model_dump(mode="json")


@router.post("/personas/evolution-proposals/{proposal_id}/approve")
async def approve_evolution_proposal(
    proposal_id: str,
    body: ProposalDecisionRequest,
    request: Request,
):
    service = _service(request)
    try:
        result = await service.approve_evolution_proposal(
            proposal_id,
            actor=body.actor,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except (EvolutionGuardError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    return result.model_dump(mode="json")


@router.post("/personas/evolution-proposals/{proposal_id}/reject")
async def reject_evolution_proposal(
    proposal_id: str,
    body: ProposalDecisionRequest,
    request: Request,
):
    service = _service(request)
    try:
        proposal = await service.reject_evolution_proposal(
            proposal_id,
            actor=body.actor,
            reason=body.reason,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return proposal.model_dump(mode="json")


@router.post("/personas/instances/{tenant_id}/{user_id}/{instance_id}/rollback")
async def rollback_evolution(
    tenant_id: str,
    user_id: str,
    instance_id: str,
    body: RollbackRequest,
    request: Request,
):
    """Reverse the changes recorded under ``delta_id``.

    Bumps overlay_version; appends a new audit row marking the rollback.
    """
    service = _service(request)
    try:
        result = await service.rollback_evolution(
            tenant_id=tenant_id,
            user_id=user_id,
            instance_id=instance_id,
            delta_id=body.delta_id,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return result.model_dump(mode="json")


@router.delete("/personas/instances/{tenant_id}/{user_id}/{instance_id}")
async def delete_instance(tenant_id: str, user_id: str, instance_id: str, request: Request):
    service = _service(request)
    await service.delete_instance(tenant_id=tenant_id, user_id=user_id, instance_id=instance_id)
    return {"deleted": instance_id}


@router.post("/personas/instances/{tenant_id}/{user_id}/{instance_id}/compile-preview")
async def compile_preview(
    tenant_id: str,
    user_id: str,
    instance_id: str,
    body: CompilePreviewRequest,
    request: Request,
):
    service = _service(request)
    compiled = await service.compile_prompt(
        tenant_id=tenant_id,
        user_id=user_id,
        instance_id=instance_id,
        template_id=body.template_id,
        user_text=body.user_text,
        realtime=body.realtime,
        dry_run_memory=_hits(body.memory_hits),
    )
    return compiled.model_dump(mode="json")


@router.post("/personas/instances/{tenant_id}/{user_id}/{instance_id}/evolve")
async def evolve_instance(
    tenant_id: str,
    user_id: str,
    instance_id: str,
    body: EvolveRequest,
    request: Request,
):
    service = _service(request)
    result = await service.evolve(
        tenant_id=tenant_id,
        user_id=user_id,
        instance_id=instance_id,
        template_id=body.template_id,
        events=[PersonaEvolutionEvent(**event) for event in body.events],
        dry_run=body.dry_run,
    )
    return result.model_dump(mode="json")


@router.post("/personas/instances/{tenant_id}/{user_id}/{instance_id}/mock-memory-trigger")
async def mock_memory_trigger(
    tenant_id: str,
    user_id: str,
    instance_id: str,
    body: MockMemoryTriggerRequest,
    request: Request,
):
    service = _service(request)
    result = await service.mock_memory_trigger(
        tenant_id=tenant_id,
        user_id=user_id,
        instance_id=instance_id,
        template_id=body.template_id,
        user_text=body.user_text,
        memory_hits=_hits(body.memory_hits) or [],
        apply=body.apply,
    )
    return result.model_dump(mode="json")


def _service(request: Request):
    service = request.app.state.personas_service
    if service is None:
        raise HTTPException(status_code=503, detail="personas service is not configured")
    return service


def _hits(raw_hits: list[dict] | None) -> list[MemoryHit] | None:
    if raw_hits is None:
        return None
    hits: list[MemoryHit] = []
    for idx, raw in enumerate(raw_hits):
        kind = raw.get("kind", "fragment")
        metadata = raw.get("metadata") or {}
        kind_value = MemoryKind(kind) if isinstance(kind, str) else kind
        hits.append(
            MemoryHit(
                id=str(raw.get("id") or f"mock-{idx}"),
                content=str(raw.get("content") or raw.get("value") or ""),
                kind=kind_value,
                similarity=float(raw.get("similarity", 1.0)),
                metadata=metadata,
            )
        )
    return hits
