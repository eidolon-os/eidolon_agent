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

from eidolon_agent.core.ports.events import EventBus
from eidolon_agent.core.ports.long_tasks import LongTaskQueueFullError
from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.long_task import LongTaskRecord, LongTaskStatus
from eidolon_agent.core.types.topics import Topics

_log = logging.getLogger(__name__)

_TERMINAL_SUCCESS = {"RUN_END", "RUN_FINISHED"}
_TERMINAL_FAILURE = {"RUN_FAILED", "RUN_CANCELLED", "ERROR"}


def _device_id_from_conversation_id(conversation_id: str | None) -> str | None:
    """Best-effort device_id from a livekit conversation_id.

    Channel builds ``conversation_id = "<prefix>:<participant_identity>:<room>"``
    (e.g. ``livekit:1c:db:d4:7a:ef:0c:device-1c-db-d4-7a-ef-0c``). The identity
    (a MAC) contains colons; the prefix and room do not — so the identity is the
    middle, i.e. everything between the first and last ``:`` segments. This is a
    transitional fallback used until ``device_id`` is wired end-to-end onto the
    record (caller identity → turns.source_device_id / long_tasks.device_id). Returns
    None when the shape doesn't match (then the proactive wake is simply skipped
    upstream).
    """
    if not conversation_id:
        return None
    parts = conversation_id.split(":")
    if len(parts) < 3:
        return None
    device_id = ":".join(parts[1:-1]).strip()
    return device_id or None


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


class LongTaskStorePort(Protocol):
    async def accept(self, record: LongTaskRecord) -> None:
        ...

    async def mark_failed(
        self,
        task_id: str,
        *,
        error_code: str,
        error_message: str,
        error_payload: dict | None = None,
        status: LongTaskStatus = LongTaskStatus.FAILED,
    ) -> LongTaskRecord | None:
        ...

    async def mark_queued(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_until: datetime | None = None,
    ) -> LongTaskRecord | None:
        ...

    async def find_mementos_session_id(self, session_key: str) -> str | None:
        ...

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
        ...

    async def touch_poll(
        self,
        task_id: str,
        *,
        latest_seq: int | None = None,
        external_status: str | None = None,
    ) -> LongTaskRecord | None:
        ...

    async def complete(
        self,
        task_id: str,
        *,
        result_text: str | None = None,
        result_payload: dict | None = None,
        artifact_paths: list[str] | None = None,
    ) -> LongTaskRecord | None:
        ...

    async def set_result_tts_summary(
        self, task_id: str, summary: str
    ) -> LongTaskRecord | None:
        ...

    async def claim_callback_delivery(self, task_id: str, *, subject: str) -> bool:
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
        store: LongTaskStorePort,
        client: MementosHttpClient,
        config: MementosWorkerConfig | None = None,
        result_summarizer: LongTaskResultSummarizerPort | None = None,
        persona_voice=None,
        event_bus: EventBus | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._config = config or MementosWorkerConfig()
        self._result_summarizer = result_summarizer
        # PersonaVoice (optional): frames the proactive report in the
        # companion's voice and guarantees the fallback is never a raw dump.
        self._persona_voice = persona_voice
        self._event_bus = event_bus
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
                    summary = await self._summarize_result_for_tts(record, content)
                    await self._publish_proactive_report(record, summary, content)
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
    ) -> str | None:
        if self._result_summarizer is None or not result_text.strip():
            return None
        try:
            summary = await self._result_summarizer.summarize(record, result_text)
        except Exception:
            _log.exception("long task result TTS summary failed: task_id=%s", record.id)
            return None
        if summary:
            await self._store.set_result_tts_summary(record.id, summary)
        return summary or None

    async def _publish_proactive_report(
        self,
        record: LongTaskRecord,
        summary: str | None,
        result_text: str,
    ) -> None:
        """Announce a finished task so the companion can speak it unprompted.

        Publishes ``agent.proactive.triggered.<companion_id>`` carrying the
        spoken text. The publish is gated on an atomic callback claim so a
        completion seen more than once is announced exactly once. Anything that
        prevents a clean announcement (no bus, no companion, empty text, store or
        bus error) is logged and skipped — it must never fail the task.
        """
        if self._event_bus is None:
            return
        companion_id = record.companion_id
        if not companion_id:
            _log.info(
                "proactive report skipped: no companion_id task_id=%s",
                record.id,
            )
            return
        # Persona-frame the proactive utterance. ``summary`` is already spoken
        # in the companion's voice; when it is missing we must NOT dump raw
        # task output over TTS — proactive_decision falls back to a clean,
        # persona-overridable line (the full result stays on the job record for
        # the user to ask about). Degrades to a plain default without persona.
        intent = "long_task_done"
        fallback_line = "我把刚才交代的那件事处理好了，细节你可以随时问我。"
        style_hint = "report"
        if self._persona_voice is not None:
            decision = await self._persona_voice.proactive_decision(
                owner_id=record.owner_id,
                companion_id=companion_id,
                intent=intent,
                primary_text=summary or "",
                fallback_default=fallback_line,
                style_hint=style_hint,
            )
            report_text = decision.text.strip()
            intent = decision.intent
            style_hint = decision.style_hint
        else:
            report_text = (summary or fallback_line).strip()
        if not report_text:
            return
        subject = Topics.proactive_triggered(companion_id)
        try:
            claimed = await self._store.claim_callback_delivery(
                record.id,
                subject=subject,
            )
        except Exception:
            _log.exception(
                "proactive report callback claim failed: task_id=%s", record.id
            )
            return
        if not claimed:
            _log.debug("proactive report already delivered: task_id=%s", record.id)
            return
        # device_id makes this event self-routing: hub just send_command(room.join)
        # to it (plan §3 Phase 3, "publisher owns the mapping; hub only sends").
        # Prefer the denormalized record field; fall back to parsing the livekit
        # conversation_id until device_id is wired end-to-end.
        device_id = record.device_id or _device_id_from_conversation_id(
            record.conversation_id
        )
        if not device_id:
            _log.info(
                "proactive report: unresolved device_id task_id=%s conversation_id=%r "
                "(wake will be skipped by orchestrator)",
                record.id,
                record.conversation_id,
            )
        event = Event(
            subject=subject,
            payload={
                "instance_id": companion_id,
                "device_id": device_id,
                "intent": intent,
                "text": report_text,
                "style_hint": style_hint,
            },
            trace_id=record.trace_id,
            source="mementos-long-task-worker",
        )
        try:
            await self._event_bus.publish(event)
        except Exception:
            _log.exception(
                "proactive report publish failed: task_id=%s subject=%s",
                record.id,
                subject,
            )


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
