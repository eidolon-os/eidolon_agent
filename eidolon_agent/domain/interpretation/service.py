"""InterpretationService — one primary interpreter, optional shadows, a replay record.

The primary answers the caller. Shadows see the same request concurrently,
each under its own timeout, and are only recorded: they can never delay,
change, or fail what the primary returned. Which adapter is primary is a
config value, so promoting a shadow (or rolling back) changes no code.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from eidolon_sdk.biz.interpretation import (
    ERROR_INVALID_PROPOSAL,
    ERROR_TIMEOUT,
    ERROR_UNAVAILABLE,
    InterpretationError,
    InterpretationRequest,
    InterpretationResult,
    validate_proposal,
)

from eidolon_agent.core.ports.interpretation import (
    InteractionInterpretationPort,
    InterpretationRecorderPort,
)
from eidolon_agent.core.types.interpretation import (
    RUN_CANCELLED,
    RUN_EXCEPTION,
    AdapterRole,
    AdapterRun,
    InterpretationRecord,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InterpretationConfig:
    """Which registered adapter answers, and which only run in the shadow."""

    primary: str
    shadows: tuple[str, ...] = ()
    shadow_timeout_s: float = 2.0


class InterpretationService:
    """Implements :class:`InteractionInterpretationPort` over named adapters.

    Every result passes ``validate_proposal``; a result that fails it is
    returned as ``abstained`` (the rejected original stays in the record).
    Primary errors are recorded and re-raised as ``InterpretationError``.
    """

    def __init__(
        self,
        adapters: Mapping[str, InteractionInterpretationPort],
        config: InterpretationConfig,
        *,
        recorder: InterpretationRecorderPort | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        names = (config.primary, *config.shadows)
        unknown = [name for name in names if name not in adapters]
        if unknown:
            raise ValueError(f"interpretation adapters not registered: {', '.join(unknown)}")
        if len(set(names)) != len(names):
            raise ValueError("an interpretation adapter is listed twice in the config")
        if config.shadow_timeout_s <= 0:
            raise ValueError("shadow_timeout_s must be positive")
        self._primary_name = config.primary
        self._primary = adapters[config.primary]
        self._shadows = tuple((name, adapters[name]) for name in config.shadows)
        self._shadow_timeout_s = config.shadow_timeout_s
        self._recorder = recorder
        self._clock = clock
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def primary(self) -> str:
        return self._primary_name

    async def interpret(self, request: InterpretationRequest) -> InterpretationResult:
        # Shadows start first so they overlap the primary, not trail it.
        shadow_runs = [
            asyncio.create_task(self._run_shadow(name, adapter, request))
            for name, adapter in self._shadows
        ]
        try:
            run, answer = await self._run_primary(request)
        except asyncio.CancelledError:
            run = AdapterRun(self._primary_name, "primary", 0.0, error=RUN_CANCELLED)
            self._finish(request, run, shadow_runs)
            raise
        self._finish(request, run, shadow_runs)
        if isinstance(answer, InterpretationError):
            raise answer
        return answer

    async def drain(self) -> None:
        """Wait for in-flight shadows and record writes (tests, shutdown)."""
        while self._pending:
            tasks = tuple(self._pending)
            await asyncio.gather(*tasks, return_exceptions=True)
            # gather of finished tasks does not yield, so the discard callbacks
            # may not have run yet; drop them here or this loop never ends.
            self._pending.difference_update(tasks)

    async def _run_primary(
        self, request: InterpretationRequest
    ) -> tuple[AdapterRun, InterpretationResult | InterpretationError]:
        start = self._clock()
        try:
            async with asyncio.timeout(request.timeout_ms / 1000):
                result = await self._primary.interpret(request)
        except TimeoutError:
            run = self._failed(self._primary_name, "primary", start, ERROR_TIMEOUT, "deadline")
            return run, InterpretationError(
                ERROR_TIMEOUT, f"{self._primary_name} exceeded deadline"
            )
        except InterpretationError as exc:
            return self._failed(self._primary_name, "primary", start, exc.code, str(exc)), exc
        except Exception as exc:
            _log.exception("interpretation adapter %s raised", self._primary_name)
            run = self._failed(self._primary_name, "primary", start, RUN_EXCEPTION, repr(exc))
            return run, InterpretationError(ERROR_UNAVAILABLE, f"{self._primary_name} failed")
        return self._checked(self._primary_name, "primary", start, request, result)

    async def _run_shadow(
        self, name: str, adapter: InteractionInterpretationPort, request: InterpretationRequest
    ) -> AdapterRun:
        start = self._clock()
        try:
            async with asyncio.timeout(self._shadow_timeout_s):
                result = await adapter.interpret(request)
        except TimeoutError:
            return self._failed(name, "shadow", start, ERROR_TIMEOUT, "shadow deadline")
        except InterpretationError as exc:
            return self._failed(name, "shadow", start, exc.code, str(exc))
        except Exception as exc:
            _log.warning("shadow interpreter %s raised: %r", name, exc)
            return self._failed(name, "shadow", start, RUN_EXCEPTION, repr(exc))
        run, _answer = self._checked(name, "shadow", start, request, result)
        return run

    def _checked(
        self,
        name: str,
        role: AdapterRole,
        start: float,
        request: InterpretationRequest,
        result: object,
    ) -> tuple[AdapterRun, InterpretationResult | InterpretationError]:
        latency_ms = (self._clock() - start) * 1000
        if not isinstance(result, InterpretationResult):
            detail = f"returned {type(result).__name__}"
            run = AdapterRun(name, role, latency_ms, error=ERROR_INVALID_PROPOSAL, detail=detail)
            return run, InterpretationError(ERROR_INVALID_PROPOSAL, f"{name} {detail}")
        try:
            validate_proposal(request, result)
        except ValueError as exc:
            reason = str(exc) or ERROR_INVALID_PROPOSAL
            _log.warning("interpreter %s proposal rejected: %s", name, reason)
            abstained = InterpretationResult(
                interpretation_id=request.interpretation_id,
                status="abstained",
                policy_version=result.policy_version,
                model_version=result.model_version,
                diagnostics={"rejected": reason[:120]},
            )
            return AdapterRun(name, role, latency_ms, result=result, rejected=reason), abstained
        return AdapterRun(name, role, latency_ms, result=result), result

    def _failed(
        self, name: str, role: AdapterRole, start: float, error: str, detail: str
    ) -> AdapterRun:
        latency_ms = (self._clock() - start) * 1000
        return AdapterRun(name, role, latency_ms, error=error, detail=detail[:500])

    def _finish(
        self,
        request: InterpretationRequest,
        primary: AdapterRun,
        shadow_runs: list[asyncio.Task[AdapterRun]],
    ) -> None:
        if self._recorder is None and not shadow_runs:
            return
        task = asyncio.create_task(self._record(request, primary, shadow_runs))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _record(
        self,
        request: InterpretationRequest,
        primary: AdapterRun,
        shadow_runs: list[asyncio.Task[AdapterRun]],
    ) -> None:
        shadows = tuple(await asyncio.gather(*shadow_runs)) if shadow_runs else ()
        if self._recorder is None:
            return
        record = InterpretationRecord(
            recorded_at=datetime.now(UTC).isoformat(),
            request=request,
            primary=primary,
            shadows=shadows,
        )
        try:
            await self._recorder.record(record)
        except Exception as exc:
            _log.warning("interpretation record %s dropped: %r", request.interpretation_id, exc)
