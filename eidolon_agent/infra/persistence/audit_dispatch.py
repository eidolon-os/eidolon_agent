"""Failure-isolated publisher for the Agent authority's local audit outbox."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from eidolon_sdk.biz.audit import AuditPublisher
from eidolon_sdk.integrations.audit import (
    AuditNatsPublisherSettings,
    JetStreamAuditPublisher,
)

from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore

logger = logging.getLogger(__name__)

_PUBLISHED_RETENTION = timedelta(hours=24)
_PURGE_INTERVAL_SECONDS = 60 * 60


class AgentAuditDispatcher:
    def __init__(
        self,
        store: AgentRuntimeStore,
        publisher: AuditPublisher,
        *,
        batch_size: int = 200,
    ) -> None:
        self._outbox = store.audit_outbox
        self._publisher = publisher
        self._batch_size = batch_size

    async def dispatch_once(self) -> int:
        batch = await self._outbox.pending_batch(limit=self._batch_size)
        if not batch.events:
            return 0
        event_ids = {event.event_id for event in batch.events}
        retry_after = timedelta(seconds=min(2 ** min(max(batch.max_attempt_count, 0), 16), 60))
        try:
            acknowledged = await self._publisher.publish_many(batch.events)
        except Exception as exc:  # transport failure must not kill Agent
            await self._outbox.mark_failed(
                event_ids,
                error=f"{type(exc).__name__}: {exc}",
                retry_after=retry_after,
            )
            return 0
        acknowledged &= event_ids
        await self._outbox.mark_published(acknowledged)
        missing = event_ids - acknowledged
        if missing:
            await self._outbox.mark_failed(
                missing,
                error="transport did not acknowledge event",
                retry_after=retry_after,
            )
        return len(acknowledged)


async def run_agent_audit_dispatcher(store: AgentRuntimeStore, *, nats_url: str) -> None:
    """Drain only Agent's database; no sibling authority DB is ever opened."""

    publisher = JetStreamAuditPublisher(
        AuditNatsPublisherSettings(url=nats_url),
        connection_name="eidolon-agent-audit-publisher",
    )
    dispatcher = AgentAuditDispatcher(store, publisher)
    next_purge = 0.0
    try:
        while True:
            try:
                published = await dispatcher.dispatch_once()
                now = time.monotonic()
                if now >= next_purge:
                    await store.audit_outbox.purge_published(
                        before=datetime.now(UTC) - _PUBLISHED_RETENTION
                    )
                    next_purge = now + _PURGE_INTERVAL_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("agent audit dispatcher iteration failed")
                published = 0
            if published == 0:
                await asyncio.sleep(0.25)
    finally:
        await publisher.close()


__all__ = ["AgentAuditDispatcher", "run_agent_audit_dispatcher"]
