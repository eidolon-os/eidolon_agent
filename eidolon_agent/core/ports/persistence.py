"""Repository ports for SQLite-backed persistence.

The repositories are deliberately thin — they translate between domain types
and ORM rows, nothing else. Business logic lives in services/agents.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.messages import ChatMessage
from eidolon_agent.core.types.persona import EvolutionDelta
from eidolon_agent.core.types.turn import TurnResult


@runtime_checkable
class ChatMessageRepository(Protocol):
    async def append(self, turn_id: str, message: ChatMessage) -> None: ...

    async def list_for_turn(self, turn_id: str) -> list[ChatMessage]: ...

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 200,
        before: datetime | None = None,
    ) -> list[ChatMessage]: ...

    async def delete_for_user(self, user_id: str) -> int:
        """Used by ``/admin/users/{id}/forget``. Returns rows affected."""
        ...


@runtime_checkable
class ConversationRepository(Protocol):
    async def start(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        user_id: str,
        agent_instance_id: str,
    ) -> None: ...

    async def finish(self, conversation_id: str, *, title: str | None = None) -> None: ...

    async def record_turn(self, result: TurnResult) -> None: ...


@runtime_checkable
class DeviceRepository(Protocol):
    async def register(
        self,
        *,
        device_id: str,
        tenant_id: str,
        user_id: str,
        token_hash: str,
        scopes: list[str],
    ) -> None: ...

    async def revoke(self, device_id: str) -> None: ...

    async def is_revoked(self, device_id: str) -> bool: ...

    async def touch_last_seen(self, device_id: str) -> None: ...


@runtime_checkable
class EvolutionHistoryRepository(Protocol):
    async def record(self, delta: EvolutionDelta) -> None: ...

    async def list_for_instance(
        self, instance_id: str, *, limit: int = 50
    ) -> list[EvolutionDelta]: ...

    async def get(self, delta_id: str) -> EvolutionDelta | None: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Transactional scope. Use as ``async with uow: ...``."""

    chat_messages: ChatMessageRepository
    conversations: ConversationRepository
    devices: DeviceRepository
    evolution_history: EvolutionHistoryRepository

    async def __aenter__(self) -> UnitOfWork: ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
