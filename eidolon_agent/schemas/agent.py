"""Agent 与模板相关 Schema."""

from datetime import datetime

from pydantic import BaseModel, Field


class AgentTemplateResponse(BaseModel):
    """性格基因模板（只读）."""

    id: int
    name: str
    base_prompt: str
    description: str

    model_config = {"from_attributes": True}


class AgentCreate(BaseModel):
    """创建数字生命实例."""

    template_id: int
    name: str = Field(..., min_length=1, max_length=128)


class AgentResponse(BaseModel):
    """数字生命实例响应."""

    id: int
    user_id: int
    template_id: int
    name: str
    soul_state_md: str
    evolution_level: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentEvolutionTrigger(BaseModel):
    """触发进化（可选：指定 agent_id，否则由服务按策略选）. 用于 API 或定时任务."""

    agent_id: int | None = None
