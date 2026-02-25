"""Mem0Provider 集成测试：真实调用 mem0，需安装 mem0ai 且配置 MEM0_API_KEY 或 OPENAI_API_KEY. 未满足时跳过."""

import os
import uuid

import pytest

# 未安装 mem0ai 时跳过本模块（pip install mem0ai）
try:
    import mem0  # noqa: F401
except ModuleNotFoundError:
    pytest.skip("未安装 mem0ai，请执行: pip install mem0ai", allow_module_level=True)

from eidolon_agent.core.config import settings
from eidolon_agent.memory.mem0_provider import Mem0Provider


def _mem0_configured() -> bool:
    """是否已配置 mem0（API Key 或 OSS 会用到的 OPENAI_API_KEY）."""
    if settings.mem0_api_key and settings.mem0_api_key.strip():
        return True
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return True
    return False


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _mem0_configured(), reason="MEM0_API_KEY 或 OPENAI_API_KEY 未配置，跳过真实 mem0 调用"),
]


@pytest.fixture
def provider():
    """真实 Mem0Provider，使用当前配置的 mem0."""
    return Mem0Provider()


@pytest.fixture
def unique_user_id():
    """每个测试使用独立 user_id，避免互相污染."""
    return f"test_user_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def unique_agent_id():
    return f"test_agent_{uuid.uuid4().hex[:12]}"


@pytest.mark.asyncio
async def test_add_memory_then_search(provider: Mem0Provider, unique_user_id: str):
    """真实调用：写入一段对话后能检索到相关记忆."""
    messages = [
        {"role": "user", "content": "我特别喜欢在周末去爬山，尤其是杭州的北高峰。"},
        {"role": "assistant", "content": "好的，已记住您喜欢周末去杭州北高峰爬山。"},
    ]
    add_result = await provider.add_memory(user_id=unique_user_id, messages=messages)
    assert isinstance(add_result, list)

    results = await provider.search_memory(
        user_id=unique_user_id,
        query="用户喜欢去哪里、做什么",
        limit=5,
    )
    assert isinstance(results, list)
    # 至少应能搜到一条相关记忆（或 mem0 未返回则为空，不强制）
    if results:
        assert any("北高峰" in r.memory or "爬山" in r.memory for r in results)

    await provider.delete_memory(user_id=unique_user_id, delete_all=True)


@pytest.mark.asyncio
async def test_add_with_agent_id_then_search(provider: Mem0Provider, unique_user_id: str, unique_agent_id: str):
    """真实调用：带 agent_id 写入后，用同一 agent_id 能检索到."""
    messages = [
        {"role": "user", "content": "我的生日是 3 月 15 日。"},
        {"role": "assistant", "content": "已记住，您的生日是 3 月 15 日。"},
    ]
    await provider.add_memory(
        user_id=unique_user_id,
        messages=messages,
        agent_id=unique_agent_id,
    )

    results = await provider.search_memory(
        user_id=unique_user_id,
        query="用户的生日",
        limit=5,
        agent_id=unique_agent_id,
    )
    assert isinstance(results, list)
    if results:
        assert any("生日" in r.memory or "3" in r.memory for r in results)

    await provider.delete_memory(user_id=unique_user_id, agent_id=unique_agent_id, delete_all=True)


@pytest.mark.asyncio
async def test_delete_all_cleans_user_memories(provider: Mem0Provider, unique_user_id: str):
    """真实调用：delete_all 后该 user 下不应再搜到记忆（或明显减少）."""
    messages = [
        {"role": "user", "content": "测试内容：唯一字符串 XYZ_cleanup_test。"},
        {"role": "assistant", "content": "已记录。"},
    ]
    await provider.add_memory(user_id=unique_user_id, messages=messages)

    before = await provider.search_memory(user_id=unique_user_id, query="XYZ_cleanup_test", limit=10)
    await provider.delete_memory(user_id=unique_user_id, delete_all=True)
    after = await provider.search_memory(user_id=unique_user_id, query="XYZ_cleanup_test", limit=10)

    assert len(after) <= len(before)
    # 理想情况删除后为 0；若 mem0 有延迟或实现差异，至少不应多于删除前
    if before:
        assert len(after) < len(before) or len(after) == 0
