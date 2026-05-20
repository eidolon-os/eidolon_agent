"""Unit-of-Work wrapping a single AsyncSession transaction."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eidolon_agent.persistence.repositories import (
    SqlChatMessageRepository,
    SqlConversationRepository,
    SqlDeviceRepository,
    SqlEvolutionHistoryRepository,
)


class SqlAlchemyUnitOfWork:
    """Async-context-manager wrapping a single transaction.

    Usage::

        async with uow:
            await uow.chat_messages.append(turn_id, msg)
            await uow.commit()
    """

    chat_messages: SqlChatMessageRepository
    conversations: SqlConversationRepository
    devices: SqlDeviceRepository
    evolution_history: SqlEvolutionHistoryRepository

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._session_factory()
        self.chat_messages = SqlChatMessageRepository(self._session)
        self.conversations = SqlConversationRepository(self._session)
        self.devices = SqlDeviceRepository(self._session)
        self.evolution_history = SqlEvolutionHistoryRepository(self._session)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self._session is not None
        try:
            if exc_type is not None:
                await self._session.rollback()
        finally:
            await self._session.close()
            self._session = None

    async def commit(self) -> None:
        assert self._session is not None
        await self._session.commit()

    async def rollback(self) -> None:
        assert self._session is not None
        await self._session.rollback()
