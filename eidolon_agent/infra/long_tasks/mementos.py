"""In-process long-task worker backed by the local Mementos sidecar."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx

from eidolon_agent.core.ports.long_tasks import LongTaskQueueFullError
from eidolon_agent.core.types.long_task import LongTaskRecord, LongTaskStatus
from eidolon_agent.infra.persistence.long_task_store import SqlLongTaskStore

_log = logging.getLogger(__name__)

_TERMINAL_SUCCESS = {"RUN_END", "RUN_FINISHED"}
_TERMINAL_FAILURE = {"RUN_FAILED", "RUN_CANCELLED", "ERROR"}


@dataclass(frozen=True, slots=True)
class MementosWorkerConfig:
    base_url: str = "http://127.0.0.1:18765"
    queue_size: int = 256
    poll_interval_s: float = 1.0
    task_timeout_s: float = 1800.0
    http_timeout_s: float = 30.0
    lease_s: float = 3600.0


class MementosHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s, trust_env=False)
        self._max_retries = max(0, max_retries)

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        return await self._request_json("GET", "/health")

    async def create_session(self, *, title: str) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            "/api/v1/chat/sessions",
            json_body={"title": title},
        )

    async def post_message(
        self,
        *,
        session_id: str,
        prompt: str,
        file_list: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            f"/api/v1/chat/sessions/{session_id}/messages",
            json_body={"content": prompt, "file_list": file_list or []},
        )

    async def get_messages(self, *, session_id: str, limit: int = 500) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            f"/api/v1/chat/sessions/{session_id}/messages",
            params={"limit": limit},
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(
                    method,
                    f"{self._base_url}{path}",
                    json=json_body,
                    params=params,
                )
                response.raise_for_status()
                return response.json() if response.content else {}
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                if not _should_retry(exc) or attempt >= self._max_retries:
                    raise
                await asyncio.sleep(min(2.0, 0.2 * (2**attempt)))
        assert last_exc is not None
        raise last_exc


class LongTaskResultSummarizerPort(Protocol):
    async def summarize(self, record: LongTaskRecord, result_text: str) -> str | None:
        ...


class MementosLongTaskWorker:
    """Fast submitter plus background Mementos executor.

    ``submit`` is the hot path used by the LLM tool loop: it writes a tiny
    accepted receipt, enqueues the immutable record, and returns. All HTTP and
    result polling happens in the worker task.
    """

    def __init__(
        self,
        *,
        store: SqlLongTaskStore,
        client: MementosHttpClient,
        config: MementosWorkerConfig | None = None,
        result_summarizer: LongTaskResultSummarizerPort | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._config = config or MementosWorkerConfig()
        self._result_summarizer = result_summarizer
        self._worker_id = worker_id or f"agent-{uuid.uuid4().hex[:8]}"
        self._queue: asyncio.Queue[LongTaskRecord] = asyncio.Queue(
            maxsize=self._config.queue_size
        )
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def submit(self, record: LongTaskRecord) -> None:
        await self._store.accept(record)
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull as exc:
            await self._store.mark_failed(
                record.id,
                error_code="queue_full",
                error_message="local long-task queue is full",
            )
            raise LongTaskQueueFullError("local long-task queue is full") from exc

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="mementos-long-task-worker")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._client.close()

    async def drain_once(self) -> bool:
        if self._queue.empty():
            return False
        record = await self._queue.get()
        try:
            await self._handle(record)
        finally:
            self._queue.task_done()
        return True

    async def _run(self) -> None:
        while not self._stopping.is_set():
            record = await self._queue.get()
            try:
                await self._handle(record)
            except Exception:
                _log.exception("long task worker failed: task_id=%s", record.id)
            finally:
                self._queue.task_done()

    async def _handle(self, record: LongTaskRecord) -> None:
        lease_until = datetime.now(timezone.utc) + timedelta(seconds=self._config.lease_s)
        await self._store.mark_queued(
            record.id,
            worker_id=self._worker_id,
            lease_until=lease_until,
        )
        try:
            session_id = await self._store.find_mementos_session_id(record.session_key)
            if session_id is None:
                session = await self._client.create_session(title=record.session_key)
                session_id = str((session.get("session") or {}).get("id") or "")
            if not session_id:
                raise RuntimeError("mementos session id missing")

            post_result = await self._client.post_message(
                session_id=session_id,
                prompt=_prompt_for_record(record),
                file_list=[],
            )
            conversation_id = str(post_result.get("conversation_id") or "")
            latest_seq = _int_or_none(post_result.get("latest_seq"))
            await self._store.attach_mementos_run(
                record.id,
                mementos_session_id=session_id,
                mementos_conversation_id=conversation_id or None,
                mementos_run_id=conversation_id or None,
                latest_seq=latest_seq,
                external_status=LongTaskStatus.RUNNING.value,
            )
            await self._poll_until_done(record, session_id=session_id)
        except Exception as exc:
            await self._store.mark_failed(
                record.id,
                error_code="mementos_worker_error",
                error_message=str(exc),
                error_payload={"exception_type": type(exc).__name__},
            )

    async def _poll_until_done(self, record: LongTaskRecord, *, session_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + self._config.task_timeout_s
        last_seq: int | None = None
        while asyncio.get_running_loop().time() < deadline:
            messages = await self._client.get_messages(session_id=session_id)
            events = list(messages.get("messages") or [])
            latest = _latest_seq(events)
            if latest is not None:
                last_seq = latest
            terminal = _terminal_event(events)
            if terminal is not None:
                event_type = str(terminal.get("event_type") or terminal.get("type") or "")
                payload = terminal.get("payload") if isinstance(terminal.get("payload"), dict) else {}
                content = str(payload.get("content") or "")
                if event_type in _TERMINAL_SUCCESS:
                    await self._store.complete(
                        record.id,
                        result_text=content,
                        result_payload=terminal,
                    )
                    await self._summarize_result_for_tts(record, content)
                    return
                await self._store.mark_failed(
                    record.id,
                    error_code="mementos_run_failed",
                    error_message=content or event_type,
                    error_payload=terminal,
                )
                return
            await self._store.touch_poll(
                record.id,
                latest_seq=last_seq,
                external_status=LongTaskStatus.RUNNING.value,
            )
            await asyncio.sleep(self._config.poll_interval_s)
        await self._store.mark_failed(
            record.id,
            error_code="mementos_timeout",
            error_message="mementos task timed out",
            status=LongTaskStatus.TIMED_OUT,
        )

    async def _summarize_result_for_tts(
        self,
        record: LongTaskRecord,
        result_text: str,
    ) -> None:
        if self._result_summarizer is None or not result_text.strip():
            return
        try:
            summary = await self._result_summarizer.summarize(record, result_text)
        except Exception:
            _log.exception("long task result TTS summary failed: task_id=%s", record.id)
            return
        if summary:
            await self._store.set_result_tts_summary(record.id, summary)


def _prompt_for_record(record: LongTaskRecord) -> str:
    parts = [record.task]
    if record.expected_output:
        parts.append(f"\n期望输出：{record.expected_output}")
    if record.context_summary:
        parts.append(f"\n上下文摘要：{record.context_summary}")
    return "".join(parts)


def _terminal_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        event_type = str(event.get("event_type") or event.get("type") or "")
        if event_type in _TERMINAL_SUCCESS or event_type in _TERMINAL_FAILURE:
            return event
    return None


def _should_retry(exc: httpx.HTTPStatusError | httpx.TransportError) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return exc.response.status_code >= 500


def _latest_seq(events: list[dict[str, Any]]) -> int | None:
    values = [_int_or_none(event.get("_sse_id")) for event in events]
    values.extend(_int_or_none(event.get("seq")) for event in events)
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
