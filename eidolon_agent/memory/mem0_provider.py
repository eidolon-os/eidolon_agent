"""Mem0 记忆提供者：实现 BaseMemoryProvider，多租户通过 user_id/agent_id 隔离."""

import asyncio
import json
from typing import Any

from eidolon_agent.core.config import settings
from eidolon_agent.memory.base import BaseMemoryProvider, MemorySearchResult


class Mem0Provider(BaseMemoryProvider):
    """基于 mem0ai 的默认记忆实现；同步 API 在 executor 中执行以保持异步接口."""

    def __init__(self) -> None:
        self._memory = None

    def _get_memory(self):  # noqa: ANN201
        if self._memory is None:
            from mem0 import Memory

            config: dict[str, Any] = {}
            if settings.mem0_config and settings.mem0_config != "{}":
                try:
                    config = json.loads(settings.mem0_config)
                except json.JSONDecodeError:
                    pass
            # MEM0_API_KEY：注入到 llm 与 embedder 的 config，供 OSS Memory 使用
            if settings.mem0_api_key:
                for component in ("llm", "embedder"):
                    config.setdefault(component, {})
                    if not isinstance(config[component], dict):
                        config[component] = {}
                    config[component].setdefault("config", {})
                    if not isinstance(config[component]["config"], dict):
                        config[component]["config"] = {}
                    config[component]["config"]["api_key"] = settings.mem0_api_key
            # mem0 需用 from_config(dict)，直接 Memory(config=dict) 会报 AttributeError
            self._memory = Memory.from_config(config)
        return self._memory

    async def add_memory(
        self,
        *,
        user_id: str,
        messages: list[dict[str, str]],
        metadata: dict[str, Any] | None = None,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        memory = self._get_memory()
        kwargs: dict[str, Any] = {"user_id": user_id}
        if agent_id:
            kwargs["agent_id"] = agent_id
        if metadata:
            kwargs["metadata"] = metadata
        result = await asyncio.to_thread(
            memory.add,
            messages,
            **kwargs,
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "results" in result:
            return result["results"]
        return [result] if result else []

    async def search_memory(
        self,
        *,
        user_id: str,
        query: str,
        limit: int = 10,
        agent_id: str | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[MemorySearchResult]:
        memory = self._get_memory()
        kwargs: dict[str, Any] = {"user_id": user_id, "limit": limit}
        if agent_id:
            kwargs["agent_id"] = agent_id
        if metadata_filter:
            kwargs["metadata"] = metadata_filter
        raw = await asyncio.to_thread(
            memory.search,
            query=query,
            **kwargs,
        )
        results = raw.get("results", raw) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        out: list[MemorySearchResult] = []
        for r in results:
            if isinstance(r, str):
                out.append(MemorySearchResult(memory=r))
            elif isinstance(r, dict):
                out.append(MemorySearchResult(
                    memory=r.get("memory", r.get("text", "")),
                    metadata=r.get("metadata"),
                    score=r.get("score"),
                    id=r.get("id"),
                ))
            else:
                out.append(MemorySearchResult(memory=str(r)))
        return out

    async def delete_memory(
        self,
        *,
        user_id: str,
        memory_id: str | None = None,
        delete_all: bool = False,
        agent_id: str | None = None,
    ) -> None:
        memory = self._get_memory()
        if delete_all or memory_id is None:
            kwargs: dict[str, Any] = {"user_id": user_id}
            if agent_id:
                kwargs["agent_id"] = agent_id
            # 开源 Memory 可能为 delete_all；云 API 的 client 也为 delete_all
            delete_fn = getattr(memory, "delete_all", None) or getattr(memory, "delete_by_filter", None)
            if delete_fn:
                await asyncio.to_thread(delete_fn, **kwargs)
        else:
            await asyncio.to_thread(memory.delete, memory_id=memory_id)
