"""Convenience constructor for hooks declared from a plain async function."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from eidolon_agent.core.ports.hooks import HookEvent, HookOutcome, HookPayload, HookResult


class CommandHook:
    """Wrap a plain async function into a :class:`HookPort`-compatible object."""

    def __init__(
        self,
        *,
        event: HookEvent,
        name: str,
        priority: int,
        func: Callable[[HookPayload], Awaitable[HookResult | None]],
    ) -> None:
        self._event = event
        self._name = name
        self._priority = priority
        self._func = func

    @property
    def event(self) -> HookEvent:
        return self._event

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def handle(self, payload: HookPayload) -> HookResult:
        result = await self._func(payload)
        return result if result is not None else HookResult(outcome=HookOutcome.CONTINUE)
