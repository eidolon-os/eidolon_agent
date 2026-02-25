"""FastAPI 依赖：DB 会话、当前用户、记忆提供者."""

from collections.abc import AsyncGenerator

from fastapi import Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from eidolon_agent.core.database import async_session_factory
from eidolon_agent.memory.base import BaseMemoryProvider
from eidolon_agent.memory.mem0_provider import Mem0Provider


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """注入异步 DB 会话."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


def get_memory_provider() -> BaseMemoryProvider:
    """注入记忆提供者（当前默认 Mem0，可改为从配置或容器解析）. 不硬编码具体实现."""
    return Mem0Provider()


# 多租户：从请求头获取当前用户 ID（开发用；生产应改为 JWT 等）
CURRENT_USER_ID_HEADER = "x-user-id"


async def get_current_user_id(x_user_id: str | None = Header(None, alias=CURRENT_USER_ID_HEADER)) -> int:
    """从请求头获取当前用户 ID，用于数据隔离。未提供时 401."""
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Missing x-user-id header")
    try:
        return int(x_user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid x-user-id")
