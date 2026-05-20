"""Admin: persona templates + overlays read-only."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from eidolon_agent.core.errors import NotFoundError

router = APIRouter()


@router.get("/personas/templates")
async def list_templates(request: Request):
    reg = request.app.state.template_registry
    return [t.model_dump(mode="json") for t in reg.list_all()]


@router.get("/personas/templates/{template_id}")
async def get_template(template_id: str, request: Request):
    try:
        tpl = request.app.state.template_registry.get(template_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return tpl.model_dump(mode="json")


@router.get("/personas/overlays/{tenant_id}/{user_id}/{instance_id}")
async def get_overlay(tenant_id: str, user_id: str, instance_id: str, request: Request):
    store = request.app.state.overlay_store
    try:
        overlay = store.load(tenant_id, user_id, instance_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    return overlay.model_dump(mode="json")
