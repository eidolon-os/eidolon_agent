"""LLM 客户端：带 httpx 连接池的 AsyncOpenAI 单例，支持流式响应."""

import httpx
from openai import AsyncOpenAI

from eidolon_agent.core.config import settings

# 全局 httpx 连接池（高并发友好）
HTTPX_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=30.0,
)

_http_client: httpx.AsyncClient | None = None
_async_openai: AsyncOpenAI | None = None


def get_http_client() -> httpx.AsyncClient:
    """获取全局 AsyncClient（单例，带连接池）. 应用启动时创建，关闭时关闭."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(limits=HTTPX_LIMITS, timeout=60.0)
    return _http_client


def get_async_openai() -> AsyncOpenAI:
    """获取带连接池的 AsyncOpenAI 单例."""
    global _async_openai
    if _async_openai is None:
        client = get_http_client()
        _async_openai = AsyncOpenAI(
            api_key=settings.llm_api_key or "EMPTY",
            base_url=settings.llm_base_url.rstrip("/") or None,
            http_client=client,
        )
    return _async_openai


async def close_llm_client() -> None:
    """关闭 httpx 客户端（在 FastAPI lifespan 中调用）."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
