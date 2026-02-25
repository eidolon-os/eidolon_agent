"""Agent API：创建、列表、获取、触发进化；模板列表. 所有查询带 user_id 隔离."""

from fastapi import APIRouter, Depends

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eidolon_agent.api.dependencies import get_current_user_id, get_db, get_memory_provider
from eidolon_agent.memory.base import BaseMemoryProvider
from eidolon_agent.models.agent import Agent, AgentTemplate
from eidolon_agent.schemas.agent import AgentCreate, AgentResponse, AgentTemplateResponse
from eidolon_agent.schemas.common import ApiResponse
from eidolon_agent.services.evolution_service import EvolutionService

router = APIRouter()


@router.get("/templates", response_model=ApiResponse[list[AgentTemplateResponse]])
async def list_templates(db: AsyncSession = Depends(get_db)):
    """列出所有性格基因模板（只读）."""
    result = await db.execute(select(AgentTemplate))
    templates = list(result.scalars().all())
    return ApiResponse.ok(data=[AgentTemplateResponse.model_validate(t) for t in templates])


@router.post("", response_model=ApiResponse[AgentResponse])
async def create_agent(
    body: AgentCreate,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """当前用户下创建数字生命（绑定模板）. 需带 x-user-id."""
    agent = Agent(
        user_id=user_id,
        template_id=body.template_id,
        name=body.name,
        soul_state_md="",
        evolution_level=0,
    )
    db.add(agent)
    await db.flush()
    await db.refresh(agent)
    return ApiResponse.ok(data=AgentResponse.model_validate(agent))


@router.get("", response_model=ApiResponse[list[AgentResponse]])
async def list_agents(
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户的所有 Agent."""
    result = await db.execute(select(Agent).where(Agent.user_id == user_id))
    agents = list(result.scalars().all())
    return ApiResponse.ok(data=[AgentResponse.model_validate(a) for a in agents])


@router.get("/{agent_id}", response_model=ApiResponse[AgentResponse])
async def get_agent(
    agent_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """获取单个 Agent（仅限当前用户）. 不存在则 404."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id, Agent.user_id == user_id))
    agent = result.scalar_one_or_none()
    if not agent:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Agent not found")
    return ApiResponse.ok(data=AgentResponse.model_validate(agent))


@router.post("/{agent_id}/evolve", response_model=ApiResponse[AgentResponse])
async def evolve_agent(
    agent_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    memory: BaseMemoryProvider = Depends(get_memory_provider),
):
    """触发一次进化：根据近期对话与记忆更新灵魂状态 Markdown."""
    service = EvolutionService(db=db, memory_provider=memory)
    agent = await service.run_evolution(user_id=user_id, agent_id=agent_id)
    if not agent:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Agent not found")
    return ApiResponse.ok(data=AgentResponse.model_validate(agent))
