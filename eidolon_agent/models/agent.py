"""Agent 模板与数字生命实例."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from eidolon_agent.models.base import Base


class AgentTemplate(Base):
    """性格基因模板：基础设定与 System Prompt."""

    __tablename__ = "agent_templates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(String(512), default="")

    agents: Mapped[list["Agent"]] = relationship("Agent", back_populates="template")


class Agent(Base):
    """数字生命实例：绑定用户，拥有灵魂状态与进化等级."""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("agent_templates.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    soul_state_md: Mapped[str] = mapped_column(Text, default="")
    evolution_level: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship("User", back_populates="agents")
    template: Mapped["AgentTemplate"] = relationship("AgentTemplate", back_populates="agents")
    chat_messages: Mapped[list["ChatMessage"]] = relationship("ChatMessage", back_populates="agent")

    def __repr__(self) -> str:
        return f"<Agent(id={self.id}, user_id={self.user_id}, name={self.name})>"
