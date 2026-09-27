"""Role-group transport adapter: final text in, attributed reply stream out.

Media remains in Channel. Every output is revocable and every receipt is bound
to its request and endpoint. The reader must keep running during slow generation,
backpressure and stop confirmation so PTT can always revoke the current turn.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from eidolon_sdk.biz.control.coordination_stream import (
    Close,
    OpenScene,
    Press,
    Receipt,
    Release,
    Speaking,
    Transcript,
)

from eidolon_agent.app.interaction.coordination import Member, Permit
from eidolon_agent.app.interaction.coordination.application import IpTeamApplication
from eidolon_agent.core.types.turn import TurnEventKind


@dataclass
class PendingReceipt:
    device_id: str
    future: asyncio.Future
    permit: Permit | None = None
    completion_allowed: bool = True


class CoordinationStream:
    def __init__(self, opened: OpenScene):
        self.opened = opened
        self.stream_id = uuid4().hex
        self.session = None
        self.outbound = asyncio.PriorityQueue(maxsize=128)
        self._reply_slots = asyncio.Semaphore(64)
        self._sequence = 0
        self._pending: dict[str, PendingReceipt] = {}
        self._capture_id: str | None = None
        self._transcript: asyncio.Future | None = None
        self._released = False
        self._connected = True
        self._closing: asyncio.Task | None = None
        self.closed = asyncio.Event()

    async def prepare(self, *, application: IpTeamApplication) -> None:
        prepared = await application.prepare(
            self.opened.selection,
            authenticated_owner_id=self.opened.owner_id,
            present=self.present,
            stop=self.stop,
            transcribe=self.transcribe,
        )
        self.session = prepared.session
        self.emit(
            "prepared", urgent=True, policy=prepared.policy_version, physical_devices_ready=False
        )

    def emit(self, kind, *, permit=None, urgent=False, reply_slot=False, **data):
        if not self._connected:
            raise ConnectionError("role-group stream disconnected")
        self._sequence += 1
        self.outbound.put_nowait(
            (
                0 if urgent else 1,
                self._sequence,
                permit,
                {
                    "type": kind,
                    "stream_id": self.stream_id,
                    "session_id": self.opened.selection.session_id,
                    **data,
                },
                reply_slot,
            )
        )

    async def emit_reply(self, kind, *, permit, **data):
        await self._reply_slots.acquire()
        try:
            permit.check()
            self.emit(kind, permit=permit, reply_slot=True, **data)
        except BaseException:
            self._reply_slots.release()
            raise

    async def write_to(self, send):
        while True:
            _, _, permit, frame, reply_slot = await self.outbound.get()
            if reply_slot:
                self._reply_slots.release()
            if permit is not None and not permit.current():
                continue
            if frame["type"] == "reply_end":
                pending = self._pending.get(frame["request_id"])
                if pending is not None:
                    pending.completion_allowed = True
            await send(frame)

    async def transcribe(self, capture_id):
        if capture_id != self._capture_id or self._transcript is None:
            raise ValueError("capture no longer current")
        return await self._transcript

    async def stop(self, member: Member, epoch: int):
        request_id = uuid4().hex
        pending = PendingReceipt(member.device_id, asyncio.get_running_loop().create_future())
        self._pending[request_id] = pending
        try:
            self.emit(
                "stop", urgent=True, request_id=request_id, device_id=member.device_id, epoch=epoch
            )
            if not await pending.future:
                raise RuntimeError("device refused stop")
        finally:
            self._pending.pop(request_id, None)

    async def present(self, member, prepared, events, permit):
        request_id = permit.turn_id
        pending = PendingReceipt(
            member.device_id,
            asyncio.get_running_loop().create_future(),
            permit=permit,
            completion_allowed=False,
        )
        self._pending[request_id] = pending
        try:
            await self.emit_reply(
                "reply_start",
                permit=permit,
                request_id=request_id,
                companion_id=member.companion_id,
                device_id=member.device_id,
                turn_id=permit.turn_id,
                epoch=permit.epoch,
            )
            async for event in events:
                permit.check()
                # This interface carries speech text only. Existing business tool
                # results/private trace records never become a public group message.
                if (
                    event.kind is TurnEventKind.DELTA
                    and event.data.get("role", "answer") == "answer"
                ):
                    await self.emit_reply(
                        "reply_delta",
                        permit=permit,
                        request_id=request_id,
                        turn_id=permit.turn_id,
                        epoch=permit.epoch,
                        device_id=member.device_id,
                        text=event.data.get("text", ""),
                    )
            permit.check()
            await self.emit_reply(
                "reply_end",
                permit=permit,
                request_id=request_id,
                turn_id=permit.turn_id,
                epoch=permit.epoch,
                device_id=member.device_id,
            )
            return await pending.future
        finally:
            self._pending.pop(request_id, None)

    def accept(self, frame):
        if isinstance(frame, Receipt):
            pending = self._pending.get(frame.request_id)
            if pending is None:  # late/duplicate receipt cannot advance a new request
                return
            if pending.permit is None and frame.completion_basis != "device_ack":
                raise ValueError("stop requires device acknowledgement")
            if pending.device_id != frame.device_id:
                raise ValueError("receipt endpoint mismatch")
            if frame.result == "completed" and not pending.completion_allowed:
                raise ValueError("playback completion arrived before reply end")
            if pending.permit is not None and not pending.permit.current():
                return
            if not pending.future.done():
                pending.future.set_result(frame.result == "completed")
            return
        if isinstance(frame, Speaking):
            pending = self._pending.get(frame.turn_id)
            if pending is not None and pending.permit is not None:
                if pending.device_id != frame.device_id:
                    raise ValueError("speaking endpoint mismatch")
                if pending.permit.current():
                    pending.permit.phase("speaking")
            return
        if isinstance(frame, Close):
            if self._closing is None:
                self._closing = asyncio.create_task(self.close())
            return
        if self._closing is not None:
            raise ValueError("scene is closing")
        source = self.session.input_device_id
        if isinstance(frame, Press):
            if frame.capture_id != self._capture_id:
                self.session.press(device_id=source, capture_id=frame.capture_id)
                if self._transcript is not None and not self._transcript.done():
                    self._transcript.cancel()
                self._capture_id = frame.capture_id
                self._released = False
                self.emit(
                    "capturing", urgent=True, capture_id=frame.capture_id, epoch=self.session.epoch
                )
                self._transcript = asyncio.get_running_loop().create_future()
            elif self._released:
                raise ValueError("capture id replayed after release")
        elif isinstance(frame, Release):
            if frame.capture_id == self._capture_id and not self._released:
                self._released = True
                task = self.session.release(device_id=source, capture_id=frame.capture_id)
                if task is not None:
                    task.add_done_callback(
                        lambda done, capture_id=frame.capture_id: self._round_done(done, capture_id)
                    )
        elif isinstance(frame, Transcript):
            if frame.capture_id != self._capture_id:
                return
            if not self._released:
                raise ValueError("final transcript preceded release")
            if self._transcript.done():
                if self._transcript.cancelled() or self._transcript.result() != frame.text:
                    raise ValueError("capture transcript cannot change")
            else:
                self._transcript.set_result(frame.text)

    def _round_done(self, task, capture_id):
        if self._connected and capture_id == self._capture_id and not task.cancelled():
            try:
                self.emit(
                    "state",
                    capture_id=capture_id,
                    state=self.session.state,
                    members=dict(self.session.member_states),
                    epoch=self.session.epoch,
                    outcome=self.session.outcome,
                    error_code=self.session.error_code,
                )
            except (ConnectionError, asyncio.QueueFull):
                self._connected = False
                self.closed.set()

    async def close(self):
        try:
            if self.session is not None:
                await self.session.close()
        finally:
            if self._transcript is not None and not self._transcript.done():
                self._transcript.cancel()
            self.closed.set()

    async def disconnect(self):
        self._connected = False
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_result(False)
        if self._closing is not None:
            await self._closing
        else:
            await self.close()
