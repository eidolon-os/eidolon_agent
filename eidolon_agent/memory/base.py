"""记忆模块抽象层：BaseMemoryProvider 接口，便于替换存储实现."""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class MemorySearchResult(BaseModel):
    """单条记忆检索结果."""

    memory: str
    metadata: dict[str, Any] | None = None
    score: float | None = None
    id: str | None = None


class BaseMemoryProvider(ABC):
    """记忆提供者抽象基类：add / search / delete，所有操作需带 user_id 做多租户隔离."""

    @abstractmethod
    async def add_memory(
        self,
        *,
        user_id: str,
        messages: list[dict[str, str]],
        metadata: dict[str, Any] | None = None,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """将对话/消息写入记忆。返回写入结果（如 memory_id 等）."""
        ...

    @abstractmethod
    async def search_memory(
        self,
        *,
        user_id: str,
        query: str,
        limit: int = 10,
        agent_id: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[MemorySearchResult]:
        """按自然语言 query 检索与 user_id（及可选 agent_id）相关的记忆."""
        ...

    @abstractmethod
    async def delete_memory(
        self,
        *,
        user_id: str,
        memory_id: str | None = None,
        delete_all: bool = False,
        agent_id: str | None = None,
    ) -> None:
        """删除记忆：按 memory_id 删除单条，或 delete_all=True 时删除该 user（及可选 agent）下全部."""
        ...
