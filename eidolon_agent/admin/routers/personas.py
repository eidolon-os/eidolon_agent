"""Admin: persona templates and instances."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.core.types.memory import MemoryHit, MemoryKind
from eidolon_agent.personas.types import PersonaEvolutionEvent

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


@router.get("/personas/templates")
async def list_templates(request: Request):
    service = _service(request)
    return [t.model_dump(mode="json") for t in await service.list_templates()]


@router.get("/personas/templates/{template_id}")
async def get_template(template_id: str, request: Request):
    service = _service(request)
    try:
        tpl = await service.get_template(template_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return tpl.model_dump(mode="json")


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
