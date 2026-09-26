"""InteractionInterpretation — what one input asks for, as a proposal only.

The request/result DTOs are the SDK's ``biz.interpretation`` v1 contract, so any
implementation (in-process rules, a remote model service, an LLM classifier) is
replaceable without touching its callers. An implementation sees only the
request: it never reads device directories, holds tokens, or executes anything.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from eidolon_sdk.biz.interpretation import InterpretationRequest, InterpretationResult

from eidolon_agent.core.types.interpretation import InterpretationRecord


@runtime_checkable
class InteractionInterpretationPort(Protocol):
    async def interpret(self, request: InterpretationRequest) -> InterpretationResult:
        """Return a ``decided`` or ``abstained`` result for this request.

        Raise :class:`eidolon_sdk.biz.interpretation.InterpretationError` when no
        result could be produced (timeout, service down, malformed answer).
        Callers never read an error as "unrelated"; a proposal is never a grant
        to act, and callers re-check it against current state before executing.
        """
        ...


@runtime_checkable
class InterpretationRecorderPort(Protocol):
    async def record(self, record: InterpretationRecord) -> None:
        """Persist one record for offline replay. Best effort; may raise."""
        ...
