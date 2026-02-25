"""对话 API：流式聊天、历史列表（均需 user_id 隔离）."""

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from eidolon_agent.api.dependencies import get_current_user_id, get_db, get_memory_provider
from eidolon_agent.memory.base import BaseMemoryProvider
from eidolon_agent.schemas.chat import ChatStreamRequest
from eidolon_agent.schemas.common import ApiResponse
from eidolon_agent.services.chat_service import ChatService
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


@router.post("/stream", response_class=StreamingResponse)
async def chat_stream(
    body: ChatStreamRequest,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    memory: BaseMemoryProvider = Depends(get_memory_provider),
):
    """流式对话：请求体为 agent_id + message，返回 SSE 或纯文本流."""
    service = ChatService(db=db, memory_provider=memory)
    async def generate() -> str:
        async for chunk in service.stream_chat(user_id, body.agent_id, body.message):
            yield chunk
    return StreamingResponse(
        generate(),
        media_type="text/plain; charset=utf-8",
    )


# 可选：拉取某 Agent 的近期历史（分页）
@router.get("/history/{agent_id}")
async def chat_history(
    agent_id: int,
    limit: int = 50,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """获取当前用户下某 Agent 的对话历史（按时间倒序，带 user_id 过滤）."""
    from sqlalchemy import select
    from eidolon_agent.models.chat import ChatMessage
    from eidolon_agent.schemas.chat import ChatMessageResponse
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.agent_id == agent_id, ChatMessage.user_id == user_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    )
    messages = list(result.scalars().all())
    return ApiResponse.ok(data=[ChatMessageResponse.model_validate(m) for m in messages])
