"""进化服务：根据近期对话与记忆总结，用 LLM 更新 Agent 的生命状态 Markdown."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eidolon_agent.core.config import settings
from eidolon_agent.core.llm_client import get_async_openai
from eidolon_agent.memory.base import BaseMemoryProvider
from eidolon_agent.models.agent import Agent

# 进化时使用的近期对话条数（user+assistant）
EVOLUTION_RECENT_MESSAGES = 30
# 进化时记忆检索条数（用于总结）
EVOLUTION_MEMORY_LIMIT = 15


EVOLUTION_SYSTEM = """你是一位「数字生命状态记录员」。根据以下三部分输入，生成一份 Markdown 格式的「生命状态记录」。
- 当前生命状态（已有 Markdown）：可能为空或已有内容
- 近期对话摘要：用户与该数字生命的最近对话
- 记忆摘要：从长期记忆中抽取的与用户/该生命相关的要点

要求：
1. 输出为纯 Markdown，不要包含「根据以上…」等元说明。
2. 可包含：性格倾向、口吻、重要经历/记忆、与用户的关系、偏好等。
3. 在原有状态基础上自然「进化」，保留仍适用的部分，根据新对话与记忆做增删改。
4. 结构清晰，使用标题、列表等，便于后续作为 System 上下文使用。"""


EVOLUTION_USER_TEMPLATE = """## 当前生命状态（已有 Markdown）
{current_state}

## 近期对话（最近若干轮）
{recent_conversation}

## 记忆摘要
{memory_summary}

请输出更新后的完整「生命状态记录」Markdown："""


class EvolutionService:
    """根据对话与记忆，用 LLM 重写并更新 Agent 的 soul_state_md，evolution_level +1."""

    def __init__(
        self,
        db: AsyncSession,
        memory_provider: BaseMemoryProvider,
    ) -> None:
        self.db = db
        self.memory = memory_provider

    async def _get_agent(self, agent_id: int, user_id: int) -> Agent | None:
        result = await self.db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def _recent_messages_for_evolution(self, agent_id: int, user_id: int) -> str:
        from eidolon_agent.models.chat import ChatMessage
        result = await self.db.execute(
            select(ChatMessage)
            .where(ChatMessage.agent_id == agent_id, ChatMessage.user_id == user_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(EVOLUTION_RECENT_MESSAGES)
        )
        rows = list(result.scalars().all())
        rows.reverse()
        lines = []
        for m in rows:
            lines.append(f"**{m.role}**: {m.content[:500]}{'…' if len(m.content) > 500 else ''}")
        return "\n\n".join(lines) if lines else "（暂无对话）"

    async def _memory_summary(self, user_id: int, agent_id: int) -> str:
        memories = await self.memory.search_memory(
            user_id=str(user_id),
            query="关于这位用户和这位数字生命的整体记忆、偏好与重要事件",
            limit=EVOLUTION_MEMORY_LIMIT,
            agent_id=str(agent_id),
        )
        if not memories:
            return "（暂无记忆）"
        return "\n".join(f"- {m.memory}" for m in memories)

    async def run_evolution(self, user_id: int, agent_id: int) -> Agent | None:
        """执行一次进化：更新 soul_state_md，evolution_level += 1. 返回更新后的 Agent."""
        agent = await self._get_agent(agent_id, user_id)
        if not agent:
            return None
        current_state = agent.soul_state_md or "（暂无）"
        recent_conv = await self._recent_messages_for_evolution(agent_id, user_id)
        memory_summary = await self._memory_summary(user_id, agent_id)
        user_content = EVOLUTION_USER_TEMPLATE.format(
            current_state=current_state,
            recent_conversation=recent_conv,
            memory_summary=memory_summary,
        )
        client = get_async_openai()
        resp = await client.chat.completions.create(
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            messages=[
                {"role": "system", "content": EVOLUTION_SYSTEM},
                {"role": "user", "content": user_content},
            ],
        )
        new_md = (resp.choices[0].message.content or "").strip()
        agent.soul_state_md = new_md
        agent.evolution_level = (agent.evolution_level or 0) + 1
        self.db.add(agent)
        await self.db.flush()
        await self.db.refresh(agent)
        # commit 由 get_db 依赖在请求结束时统一提交
        return agent
