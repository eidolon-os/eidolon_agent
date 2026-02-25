"""对话相关 Schema."""

from datetime import datetime

from pydantic import BaseModel, Field


class ChatMessageCreate(BaseModel):
    """创建一条对话消息（通常由服务层写入，不直接由用户提交 content）."""

    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str = Field(..., min_length=1)


class ChatMessageResponse(BaseModel):
    """单条消息响应."""

    id: int
    user_id: int
    agent_id: int
    role: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatStreamRequest(BaseModel):
    """流式对话请求：当前用户、Agent、用户输入."""

    agent_id: int
    message: str = Field(..., min_length=1)
