"""Bounded composition of model-neutral participation decision ports.

The session still owns current-state checks and permits. This composition never
turns an explicit wait/finish into another attempt, and owns neither model port.
"""

from __future__ import annotations

import asyncio
import logging
import math

from eidolon_sdk.biz.participation import DecisionRequest, DecisionResult, validate_proposal

from eidolon_agent.core.ports.participation import DecisionUnavailable, ParticipationDecisionPort

_log = logging.getLogger(__name__)
_RECOVERABLE = frozenset({
    "DECISION_TIMEOUT", "DECISION_TRANSPORT_ERROR",
    "DECISION_HTTP_429", "DECISION_HTTP_502", "DECISION_HTTP_503", "DECISION_HTTP_504",
})


class FallbackParticipationDecision:
    """Try the primary once, then one fallback within the original total budget."""

    def __init__(
        self, primary: ParticipationDecisionPort, fallback: ParticipationDecisionPort,
        *, primary_timeout_ms: int,
    ) -> None:
        if type(primary_timeout_ms) is not int or primary_timeout_ms <= 0:
            raise ValueError("primary_timeout_ms must be a positive integer")
        self._primary = primary
        self._fallback = fallback
        self._primary_timeout_ms = primary_timeout_ms

    async def __call__(self, request: DecisionRequest) -> DecisionResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_ms / 1000
        try:
            async with asyncio.timeout_at(deadline):
                primary_budget = min(request.timeout_ms, self._primary_timeout_ms)
                primary_request = request.model_copy(update={"timeout_ms": primary_budget})
                try:
                    async with asyncio.timeout(primary_budget / 1000):
                        result = await self._primary(primary_request)
                    _validate(request, result)
                except TimeoutError:
                    reason = "DECISION_TIMEOUT"
                except DecisionUnavailable as exc:
                    if exc.code not in _RECOVERABLE:
                        raise
                    reason = exc.code
                else:
                    if result.status == "decided":
                        return result
                    reason = "abstained"

                # Give caller cancellation a chance before dispatching another model.
                await asyncio.sleep(0)
                remaining_ms = math.floor((deadline - loop.time()) * 1000)
                if remaining_ms < 1:
                    raise DecisionUnavailable("DECISION_TIMEOUT")
                _log.info(
                    "participation decision=%s route=llm_fallback reason=%s remaining_ms=%d",
                    request.decision_id, reason, remaining_ms,
                )
                fallback_request = request.model_copy(update={"timeout_ms": remaining_ms})
                result = await self._fallback(fallback_request)
                _validate(request, result)
                return result
        except TimeoutError as exc:
            raise DecisionUnavailable("DECISION_TIMEOUT") from exc


def _validate(request: DecisionRequest, result: DecisionResult) -> None:
    try:
        validate_proposal(request, result)
    except ValueError as exc:
        raise DecisionUnavailable("DECISION_INVALID_RESULT") from exc
