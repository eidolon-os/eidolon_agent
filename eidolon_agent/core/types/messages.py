"""Chat message — the unit of LLM I/O.

The same shape is persisted to SQLite ``chat_messages`` and serialized over
gRPC. Tool messages keep their structured payload so the LLM can ingest them
verbatim and the audit log can replay them faithfully.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"
    PROACTIVE = "proactive"  # assistant-initiated turn (distinguished for audit)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A single message in a Turn. Immutable; appending = new instance."""

    id: str  # uuid7 — time-sortable
    role: MessageRole
    content: str
    created_at: datetime
    content_type: str = "text/plain"
    tokens: int | None = None
    model: str | None = None  # only for assistant
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: dict | None = None
    metadata: dict = field(default_factory=dict)

    def with_content(self, content: str) -> ChatMessage:
        """Return a copy with content replaced (immutability-friendly mutation)."""
        return ChatMessage(
            id=self.id,
            role=self.role,
            content=content,
            created_at=self.created_at,
            content_type=self.content_type,
            tokens=self.tokens,
            model=self.model,
            tool_call_id=self.tool_call_id,
            tool_name=self.tool_name,
            tool_arguments=self.tool_arguments,
            metadata=self.metadata,
        )
