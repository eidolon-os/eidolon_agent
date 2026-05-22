"""Hook routing — runs registered hooks for a given lifecycle event.

Per spec: hooks are pure routing. Business logic lives in the hooks themselves.
This module contains no policy: just priority ordering, outcome dispatch, and
metrics/tracing scaffolding (added later via observability).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from eidolon_agent.core.ports.hooks import (
    HookEvent,
    HookOutcome,
    HookPayload,
    HookPort,
    HookResult,
)

_log = logging.getLogger(__name__)


class HookExecutor:
    """Registry + dispatcher for hooks. One instance per AgentInstance."""

    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[HookPort]] = {ev: [] for ev in HookEvent}

    def register(self, hook: HookPort) -> None:
        bucket = self._hooks[hook.event]
        bucket.append(hook)
        bucket.sort(key=lambda h: h.priority)

    def deregister(self, hook: HookPort) -> None:
        bucket = self._hooks.get(hook.event, [])
        if hook in bucket:
            bucket.remove(hook)

    def list_for(self, event: HookEvent) -> list[HookPort]:
        return list(self._hooks.get(event, ()))

    async def run(self, event: HookEvent, payload: HookPayload) -> HookResult:
        """Walk hooks for ``event`` in priority order.

        Stops on the first ``SHORT_CIRCUIT`` or ``ABORT``. ``MUTATE`` updates the
        payload (mutations are made on ``payload.data`` in-place by hooks).
        """
        last: HookResult = HookResult(outcome=HookOutcome.CONTINUE)
        for hook in self._hooks.get(event, ()):  # already sorted
            try:
                last = await hook.handle(payload)
            except Exception as exc:
                _log.exception("hook %s on %s raised", hook.name, event)
                return HookResult(
                    outcome=HookOutcome.ABORT,
                    reason=f"hook {hook.name} raised: {exc}",
                )
            if last.outcome in (HookOutcome.SHORT_CIRCUIT, HookOutcome.ABORT):
                return last
        return last

    def register_many(self, hooks: Iterable[HookPort]) -> None:
        for h in hooks:
            self.register(h)
