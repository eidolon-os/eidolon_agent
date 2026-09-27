"""Model-neutral participation inference; proposals carry no execution rights."""

from typing import Protocol

from eidolon_sdk.biz.participation import DecisionRequest, DecisionResult


class ParticipationDecisionPort(Protocol):
    async def __call__(self, request: DecisionRequest) -> DecisionResult: ...


class DecisionUnavailable(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
