"""NATS-backed EventBus + KVStore.

- Core NATS for transient pub/sub (low latency, no persistence).
- JetStream for subjects under :data:`eidolon_agent.core.types.topics.PERSISTENT_PREFIXES`.
- JetStream KV for the bucket-scoped KV store.

The connection is established lazily on first use so importing this module is
side-effect-free (useful for `--help` flows that don't need NATS).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import nats
import nats.errors
import nats.js
import nats.js.errors
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg as NatsMsg
from nats.js import JetStreamContext
from nats.js.api import KeyValueConfig, StreamConfig
from nats.js.kv import KeyValue

from eidolon_agent.core.errors import ConflictError, NatsUnavailableError
from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.topics import is_persistent

_log = logging.getLogger(__name__)


# Streams we own. Each captures one logical class of persistent events.
_OWNED_STREAMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("EIDOLON_EMOTION_OUT", ("agent.emotion.>",)),
)


class NatsEventBus:
    """Production EventBus backed by NATS (core + JetStream)."""

    def __init__(self, url: str, *, creds_path: str | None = None) -> None:
        self._url = url
        self._creds_path = creds_path
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def connect(self) -> None:
        async with self._lock:
            if self._nc is not None and self._nc.is_connected:
                return
            try:
                self._nc = await nats.connect(
                    self._url,
                    user_credentials=self._creds_path,
                    name="eidolon-agent",
                    connect_timeout=5,
                    max_reconnect_attempts=-1,
                )
            except Exception as exc:
                raise NatsUnavailableError(f"connect {self._url} failed: {exc}") from exc
            self._js = self._nc.jetstream()
            await self._ensure_streams()

    async def _ensure_streams(self) -> None:
        assert self._js is not None
        for name, subjects in _OWNED_STREAMS:
            try:
                await self._js.add_stream(StreamConfig(name=name, subjects=list(subjects)))
            except nats.js.errors.BadRequestError:
                # Stream already exists with different config — leave existing alone.
                pass
            except nats.js.errors.APIError as e:
                _log.warning("ensure_stream %s: %s", name, e)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            if self._nc is not None:
                await self._nc.drain()
                self._nc = None
                self._js = None

    # ---- EventBus protocol ---------------------------------------------------

    async def publish(self, event: Event, *, persistent: bool = False) -> None:
        await self.connect()
        assert self._nc is not None and self._js is not None
        payload = json.dumps(event.payload, default=str).encode()
        headers: dict[str, str] = {}
        if event.trace_id:
            headers["X-Trace-Id"] = event.trace_id
        if event.source:
            headers["X-Source"] = event.source
        # Force JetStream for subjects we own as persistent regardless of flag.
        use_js = persistent or is_persistent(event.subject)
        if use_js:
            # Msg-Id provides JetStream-level dedup.
            msg_id = event.metadata.get("msg_id") or event.metadata.get("turn_id")
            if msg_id:
                headers["Nats-Msg-Id"] = str(msg_id)
            await self._js.publish(event.subject, payload, headers=headers)
        else:
            await self._nc.publish(event.subject, payload, headers=headers or None)

    async def subscribe(
        self,
        subject: str,
        handler: Callable[[Event], Awaitable[None]],
        *,
        durable: str | None = None,
        queue_group: str | None = None,
    ) -> Callable[[], Awaitable[None]]:
        await self.connect()
        assert self._nc is not None and self._js is not None

        async def _cb(msg: NatsMsg) -> None:
            try:
                payload = json.loads(msg.data.decode()) if msg.data else {}
                ev = Event(
                    subject=msg.subject,
                    payload=payload,
                    trace_id=(msg.headers or {}).get("X-Trace-Id"),
                    source=(msg.headers or {}).get("X-Source"),
                )
                await handler(ev)
            except Exception:
                _log.exception("subscriber for %s failed", msg.subject)
            finally:
                # JetStream consumers need explicit ack; core NATS msgs ignore it.
                with contextlib_suppress(Exception):
                    await msg.ack()

        if is_persistent(subject) or durable is not None:
            sub = await self._js.subscribe(subject, durable=durable, cb=_cb, queue=queue_group)
        else:
            sub = await self._nc.subscribe(subject, cb=_cb, queue=queue_group)

        async def _unsub() -> None:
            await sub.unsubscribe()

        return _unsub

    async def request(self, subject: str, payload: dict, *, timeout_s: float = 5.0) -> dict:
        await self.connect()
        assert self._nc is not None
        try:
            resp = await self._nc.request(
                subject, json.dumps(payload, default=str).encode(), timeout=timeout_s
            )
        except nats.errors.TimeoutError as exc:
            raise asyncio.TimeoutError(str(exc)) from exc
        return json.loads(resp.data.decode()) if resp.data else {}

    async def health(self) -> bool:
        return self._nc is not None and self._nc.is_connected


# Tiny helper: contextlib.suppress is sync, but our ack is async. Use a wrapper.
class contextlib_suppress:
    def __init__(self, *excs: type[BaseException]) -> None:
        self._excs = excs or (Exception,)

    def __enter__(self) -> None: ...

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[override]
        return exc_type is not None and issubclass(exc_type, self._excs)


# ---------------------------------------------------------------------------
# JetStream KV
# ---------------------------------------------------------------------------


class NatsKVStore:
    """Single-bucket JetStream-backed KV.

    Buckets are pre-created at bootstrap (see :func:`ensure_buckets`).
    """

    def __init__(self, bus: NatsEventBus, bucket: str) -> None:
        self._bus = bus
        self.bucket = bucket
        self._kv: KeyValue | None = None
        self._lock = asyncio.Lock()

    async def _bind(self) -> KeyValue:
        async with self._lock:
            if self._kv is not None:
                return self._kv
            await self._bus.connect()
            assert self._bus._js is not None
            try:
                self._kv = await self._bus._js.key_value(self.bucket)
            except nats.js.errors.BucketNotFoundError as exc:
                raise NatsUnavailableError(
                    f"KV bucket {self.bucket!r} not found — run ensure_buckets at bootstrap"
                ) from exc
            return self._kv

    async def get(self, key: str) -> bytes | None:
        kv = await self._bind()
        try:
            entry = await kv.get(key)
        except nats.js.errors.KeyNotFoundError:
            return None
        return entry.value

    async def put(self, key: str, value: bytes, *, ttl_s: int | None = None) -> int:
        kv = await self._bind()
        # NATS KV TTL applies bucket-wide, not per-key in standard nats-py; we
        # honour ttl_s only for compatibility — caller should design buckets per TTL.
        del ttl_s
        return await kv.put(key, value)

    async def delete(self, key: str) -> None:
        kv = await self._bind()
        await kv.delete(key)

    async def cas(self, key: str, value: bytes, *, expected_revision: int) -> int:
        kv = await self._bind()
        try:
            return await kv.update(key, value, last=expected_revision)
        except nats.js.errors.APIError as exc:
            raise ConflictError(f"cas failed on {key}: {exc}") from exc

    async def keys(self, prefix: str = "") -> list[str]:
        kv = await self._bind()
        out: list[str] = []
        async for entry in kv.history(""):  # walk the bucket
            if entry.key.startswith(prefix):
                out.append(entry.key)
        # dedup; history yields versions
        return sorted(set(out))

    async def watch(self, key_pattern: str) -> AsyncIterator[tuple[str, bytes | None, int]]:
        kv = await self._bind()
        watcher = await kv.watch(keys=key_pattern, include_history=False)
        try:
            async for entry in watcher:
                if entry is None:
                    continue
                value = None if entry.operation == "DEL" else entry.value
                yield entry.key, value, entry.revision
        finally:
            await watcher.stop()


async def ensure_buckets(bus: NatsEventBus, bucket_names: list[str]) -> None:
    """Create JetStream KV buckets that don't already exist. Idempotent."""
    await bus.connect()
    assert bus._js is not None
    for name in bucket_names:
        try:
            await bus._js.create_key_value(KeyValueConfig(bucket=name))
        except nats.js.errors.BadRequestError:
            pass  # already exists with different config — accept


__all__ = ["NatsEventBus", "NatsKVStore", "ensure_buckets"]
