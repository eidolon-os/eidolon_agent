"""Streaming helpers — bounded queue, cancellation-friendly forwarder."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from eidolon_agent.core.errors import SlowConsumerError


class BoundedStream:
    """Producer/consumer queue with bounded depth.

    The Turn pipeline produces TurnEvents into this; the gRPC servicer consumes
    them. When the consumer is too slow (queue full for too long) we abort with
    SlowConsumerError instead of buffering indefinitely.
    """

    def __init__(self, *, max_depth: int = 64, slow_consumer_timeout_s: float = 5.0) -> None:
        self._q: asyncio.Queue = asyncio.Queue(maxsize=max_depth)
        self._slow_timeout = slow_consumer_timeout_s
        self._closed = False

    async def put(self, item) -> None:  # type: ignore[no-untyped-def]
        if self._closed:
            raise RuntimeError("stream already closed")
        try:
            await asyncio.wait_for(self._q.put(item), timeout=self._slow_timeout)
        except asyncio.TimeoutError as exc:
            raise SlowConsumerError(
                f"queue full for {self._slow_timeout}s — consumer lagging"
            ) from exc

    async def close(self) -> None:
        self._closed = True
        await self._q.put(None)

    async def __aiter__(self) -> AsyncIterator:
        while True:
            item = await self._q.get()
            if item is None:
                return
            yield item
