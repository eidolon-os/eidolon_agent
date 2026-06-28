"""Repository ports for SQLite-backed persistence.

The repositories are deliberately thin — they translate between domain types
and ORM rows, nothing else. Business logic lives in services/agents.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.messages import ChatMessage
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

    async def delete_for_owner(self, owner_id: str) -> int:
        """Used by owner-level privacy deletion. Returns rows affected."""
        ...


@runtime_checkable
class ConversationRepository(Protocol):
    async def start(
        self,
        *,
        conversation_id: str,
        owner_id: str,
        companion_id: str,
        device_id: str,
    ) -> None: ...

    async def finish(self, conversation_id: str, *, title: str | None = None) -> None: ...

    async def record_turn(self, result: TurnResult) -> None: ...


@runtime_checkable
class DeviceRepository(Protocol):
    async def register(
        self,
        *,
        device_id: str,
        owner_id: str,
        companion_id: str,
        token_hash: str,
        scopes: list[str],
    ) -> None: ...

    async def revoke(self, device_id: str) -> None: ...

    async def is_revoked(self, device_id: str) -> bool: ...

    async def touch_last_seen(self, device_id: str) -> None: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Transactional scope. Use as ``async with uow: ...``.

    Only core-typed repositories are declared here. Domain-specific repos
    (e.g. ``PersonaEvolutionRepository`` in ``personas.ports``) are attached
    by concrete implementations and accessed via the concrete type.
    """

    chat_messages: ChatMessageRepository
    conversations: ConversationRepository
    devices: DeviceRepository

    async def __aenter__(self) -> UnitOfWork: ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
