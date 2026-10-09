"""Execution port for the single body attached to the current Chat stream."""

from typing import Protocol

from eidolon_sdk.biz.presentation.motion import MotionReceipt, MotionRequest


class MotionExecutor(Protocol):
    async def execute(self, request: MotionRequest) -> MotionReceipt: ...
