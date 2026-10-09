"""Per-turn authenticated motion request/result rendezvous."""

import asyncio
from collections.abc import Awaitable, Callable

from eidolon_sdk.biz.presentation.motion import MotionReceipt, MotionRequest


class MotionExchange:
    def __init__(self, send: Callable[[MotionRequest], Awaitable[None]]) -> None:
        self._send = send
        self._pending: dict[str, asyncio.Future[MotionReceipt]] = {}
        self._completed: dict[str, MotionReceipt] = {}

    async def execute(self, request: MotionRequest) -> MotionReceipt:
        if request.command_id in self._completed:
            return self._completed[request.command_id]
        future = asyncio.get_running_loop().create_future()
        if request.command_id in self._pending:
            return MotionReceipt(command_id=request.command_id, status="rejected", reason="BUSY")
        self._pending[request.command_id] = future
        try:
            await self._send(request)
            receipt = await asyncio.wait_for(future, timeout=10)
            self._completed[request.command_id] = receipt
            return receipt
        except TimeoutError:
            receipt = MotionReceipt(
                command_id=request.command_id, status="failed", reason="MOTION_TIMEOUT"
            )
            self._completed[request.command_id] = receipt
            return receipt
        finally:
            self._pending.pop(request.command_id, None)

    def accept(self, receipt: MotionReceipt) -> bool:
        future = self._pending.get(receipt.command_id)
        if future is None or future.done():
            return False
        future.set_result(receipt)
        return True
