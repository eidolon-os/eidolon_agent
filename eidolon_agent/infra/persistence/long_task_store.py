"""Small persistence adapter used by long-task submitters/workers."""

from __future__ import annotations

from datetime import datetime

from eidolon_agent.core.types.long_task import LongTaskRecord, LongTaskStatus
from eidolon_agent.infra.persistence.unit_of_work import SqlAlchemyUnitOfWork


class SqlLongTaskStore:
    """Transaction boundary for long-task receipt and worker updates."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def accept(self, record: LongTaskRecord) -> None:
        async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            await uow.long_tasks.create(record)
            await uow.commit()

    async def create(self, record: LongTaskRecord) -> None:
        await self.accept(record)

    async def mark_queued(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_until: datetime | None = None,
    ) -> LongTaskRecord | None:
        async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            record = await uow.long_tasks.mark_worker_claimed(
                task_id,
                worker_id=worker_id,
                lease_until=lease_until,
            )
            await uow.commit()
            return record

    async def mark_submitted(self, task_id: str) -> LongTaskRecord | None:
        async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            record = await uow.long_tasks.update_status(
                task_id,
                LongTaskStatus.SUBMITTED,
            )
            await uow.commit()
            return record

    async def find_mementos_session_id(self, session_key: str) -> str | None:
        async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            return await uow.long_tasks.find_latest_mementos_session(session_key)

    async def attach_mementos_run(
        self,
        task_id: str,
        *,
        mementos_session_id: str | None = None,
        mementos_conversation_id: str | None = None,
        mementos_run_id: str | None = None,
        latest_seq: int | None = None,
        workspace_dir: str | None = None,
        external_status: str | None = None,
    ) -> LongTaskRecord | None:
        async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            record = await uow.long_tasks.attach_mementos_run(
                task_id,
                mementos_session_id=mementos_session_id,
                mementos_conversation_id=mementos_conversation_id,
                mementos_run_id=mementos_run_id,
                latest_seq=latest_seq,
                workspace_dir=workspace_dir,
                external_status=external_status,
            )
            await uow.commit()
            return record

    async def append_progress(
        self,
        task_id: str,
        event: dict,
        *,
        summary: str | None = None,
        latest_seq: int | None = None,
    ) -> LongTaskRecord | None:
        async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            record = await uow.long_tasks.append_progress(
                task_id,
                event,
                summary=summary,
                latest_seq=latest_seq,
            )
            await uow.commit()
            return record

    async def touch_poll(
        self,
        task_id: str,
        *,
        latest_seq: int | None = None,
        external_status: str | None = None,
    ) -> LongTaskRecord | None:
        async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            record = await uow.long_tasks.touch_poll(
                task_id,
                latest_seq=latest_seq,
                external_status=external_status,
            )
            await uow.commit()
            return record

    async def complete(
        self,
        task_id: str,
        *,
        result_text: str | None = None,
        result_payload: dict | None = None,
        artifact_paths: list[str] | None = None,
    ) -> LongTaskRecord | None:
        async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            record = await uow.long_tasks.complete(
                task_id,
                result_text=result_text,
                result_payload=result_payload,
                artifact_paths=artifact_paths,
            )
            await uow.commit()
            return record

    async def set_result_tts_summary(
        self,
        task_id: str,
        summary: str,
    ) -> LongTaskRecord | None:
        async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            record = await uow.long_tasks.set_result_tts_summary(task_id, summary)
            await uow.commit()
            return record

    async def mark_failed(
        self,
        task_id: str,
        *,
        error_code: str,
        error_message: str,
        error_payload: dict | None = None,
        status: LongTaskStatus = LongTaskStatus.FAILED,
    ) -> LongTaskRecord | None:
        async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            record = await uow.long_tasks.fail(
                task_id,
                error_code=error_code,
                error_message=error_message,
                error_payload=error_payload,
                status=status,
            )
            await uow.commit()
            return record


__all__ = ["SqlLongTaskStore"]
