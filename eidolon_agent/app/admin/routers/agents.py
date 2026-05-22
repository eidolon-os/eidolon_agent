"""Admin: AgentInstance CRUD."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from eidolon_agent.core.errors import ConflictError, NotFoundError

router = APIRouter()


class StartAgentRequest(BaseModel):
    template_id: str
    tenant_id: str
    user_id: str
    nickname_alias: str | None = None
    instance_id: str | None = None


class InstanceInfo(BaseModel):
    instance_id: str
    template_id: str
    tenant_id: str
    user_id: str
    status: str
    nickname_alias: str | None


@router.post("/agents", response_model=InstanceInfo, status_code=status.HTTP_201_CREATED)
async def start_agent(body: StartAgentRequest, request: Request):
    registry = request.app.state.agent_registry
    try:
        inst = await registry.start_instance(
            template_id=body.template_id,
            tenant_id=body.tenant_id,
            user_id=body.user_id,
            nickname_alias=body.nickname_alias,
            explicit_instance_id=body.instance_id,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return _to_info(inst)


@router.get("/agents", response_model=list[InstanceInfo])
async def list_agents(request: Request):
    return [_to_info(i) for i in request.app.state.agent_registry.list_instances()]


@router.delete("/agents/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
async def stop_agent(instance_id: str, request: Request):
    await request.app.state.agent_registry.stop_instance(instance_id)
    return None


def _to_info(inst) -> InstanceInfo:  # type: ignore[no-untyped-def]
    return InstanceInfo(
        instance_id=inst.instance_id,
        template_id=inst.template_id,
        tenant_id=inst.tenant_id,
        user_id=inst.user_id,
        status=inst.status,
        nickname_alias=inst.nickname_alias,
    )
