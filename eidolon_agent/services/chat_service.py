"""对话服务：结合记忆、历史与 LLM 流式回复，并持久化消息."""

from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from eidolon_agent.core.config import settings
from eidolon_agent.core.llm_client import get_async_openai
from eidolon_agent.memory.base import BaseMemoryProvider
from eidolon_agent.models.agent import Agent
from eidolon_agent.models.chat import ChatMessage


# 历史消息条数上限（user+assistant 合计）
RECENT_MESSAGES_LIMIT = 20
# 记忆检索条数
MEMORY_SEARCH_LIMIT = 5


class ChatService:
    """处理单轮对话：查记忆、拼系统提示、调用 LLM、落库、写记忆."""

    def __init__(
        self,
        db: AsyncSession,
        memory_provider: BaseMemoryProvider,
    ) -> None:
        self.db = db
        self.memory = memory_provider

    async def _get_agent_with_template(self, agent_id: int, user_id: int) -> Agent | None:
        result = await self.db.execute(
            select(Agent)
            .options(selectinload(Agent.template))
            .where(Agent.id == agent_id, Agent.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def _recent_messages(self, agent_id: int, user_id: int, limit: int = RECENT_MESSAGES_LIMIT) -> list[dict[str, str]]:
        result = await self.db.execute(
            select(ChatMessage)
            .where(ChatMessage.agent_id == agent_id, ChatMessage.user_id == user_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())
        rows.reverse()
        return [{"role": m.role, "content": m.content} for m in rows]

    def _build_system_prompt(self, base_prompt: str, soul_state_md: str, memory_lines: list[str]) -> str:
        parts = [base_prompt]
        if soul_state_md.strip():
            parts.append("\n## 当前生命状态（Markdown）\n" + soul_state_md.strip())
        if memory_lines:
            parts.append("\n## 相关记忆\n" + "\n".join(memory_lines))
        return "\n".join(parts)

    async def save_message(self, user_id: int, agent_id: int, role: str, content: str) -> ChatMessage:
        msg = ChatMessage(user_id=user_id, agent_id=agent_id, role=role, content=content)
        self.db.add(msg)
        await self.db.flush()
        await self.db.refresh(msg)
        return msg

    async def stream_chat(
        self,
        user_id: int,
        agent_id: int,
        user_message: str,
    ) -> AsyncIterator[str]:
        """流式对话：先落库用户消息，检索记忆与历史，调用 LLM 流式输出，边收边落库 assistant（在流结束后由调用方落库）. 仅 yield 内容片段."""
        agent = await self._get_agent_with_template(agent_id, user_id)
        if not agent:
            raise ValueError("Agent not found or not owned by user")
        await self.save_message(user_id, agent_id, "user", user_message)

        recent = await self._recent_messages(agent_id, user_id)
        memories = await self.memory.search_memory(
            user_id=str(user_id),
            query=user_message,
            limit=MEMORY_SEARCH_LIMIT,
            agent_id=str(agent_id),
        )
        memory_lines = [m.memory for m in memories]

        template = agent.template
        base_prompt = template.base_prompt if template else "你是一个数字生命助手。"
        system_prompt = self._build_system_prompt(
            base_prompt,
            agent.soul_state_md or "",
            memory_lines,
        )
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(recent)
        messages.append({"role": "user", "content": user_message})

        client = get_async_openai()
        full_content: list[str] = []
        stream = await client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            stream=True,
            temperature=settings.llm_temperature,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                piece = chunk.choices[0].delta.content
                full_content.append(piece)
                yield piece

        assistant_content = "".join(full_content)
        await self.save_message(user_id, agent_id, "assistant", assistant_content)
        await self.memory.add_memory(
            user_id=str(user_id),
            messages=[
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_content},
            ],
            agent_id=str(agent_id),
        )
        # commit 由 get_db 依赖在请求结束时统一提交
