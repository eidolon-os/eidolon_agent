"""One turn's bounded rendezvous for authenticated device delivery evidence."""

from __future__ import annotations

import asyncio

from eidolon_sdk.biz.presentation import PresentationReceipt, ResponseIntent


class PresentationFeedback:
    def __init__(self) -> None:
        self.intent: ResponseIntent | None = None
        self._future: asyncio.Future | None = None

    def expect(self, intent: ResponseIntent) -> None:
        if self.intent is not None:
            raise ValueError("ONE_FINAL_RESPONSE_PER_TURN")
        self.intent = intent
        self._future = asyncio.get_running_loop().create_future()

    def accept(self, receipt: PresentationReceipt) -> bool:
        if self.intent is None or self._future is None or self._future.done():
            return False
        if (
            receipt.response_id != self.intent.response_id
            or receipt.presentation_id not in {f"face:{self.intent.turn_id}", f"head:{self.intent.turn_id}"}
        ):
            return False
        if receipt.status not in {"completed", "cancelled", "failed", "rejected"}:
            return False
        self._future.set_result(receipt)
        return True

    async def wait(self) -> PresentationReceipt | None:
        if self._future is None:
            return None
        try:
            return await asyncio.wait_for(self._future, timeout=8)
        except TimeoutError:
            return None
