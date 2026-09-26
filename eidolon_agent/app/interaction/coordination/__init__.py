"""Session-local coordination; transports and model implementations stay outside.

Membership is supplied only after Owner/device/runtime authorization. A decision
is a proposal, not a grant. Each reply receives a revocable permit and may only
finish after actual presentation completion. No microphone audio comes from a
replying member. Instances are ephemeral: reconnect creates a new session rather
than replaying old output. This module does not expose an unauthenticated API.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from eidolon_sdk.biz.participation import (
    Candidate,
    Constraints,
    Context,
    DecisionRequest,
    DecisionResult,
    Message,
    validate_proposal,
)


@dataclass(frozen=True)
class Member:
    companion_id: str
    device_id: str
    description: str = ""


@dataclass(frozen=True)
class Permit:
    """Rechecked at every output boundary, including late TTS packets."""

    turn_id: str
    epoch: int
    current: Callable[[], bool]
    report: Callable[[str], None]

    def phase(self, value: str) -> None:
        self.check()
        if value not in {"thinking", "speaking"}:
            raise ValueError("invalid reply phase")
        self.report(value)

    def check(self) -> None:
        if not self.current():
            raise asyncio.CancelledError("reply permit revoked")


@dataclass(frozen=True)
class PlayedReply:
    """Only a fully played public reply can become the next discussion input."""

    text: str
    completed: bool


@dataclass(frozen=True)
class ReplyTask:
    """Authorized scheduling facts, distinct from a frozen decision snapshot."""

    decision_id: str
    context_ref: str
    trigger: Message
    public_context: Context


@dataclass(frozen=True)
class Ports:
    decide: Callable[[DecisionRequest], Awaitable[DecisionResult]]
    # The adapter streams via existing reply/TTS facilities, checking permit
    # before each output, and waits for device playback completion, not LLM DONE.
    reply: Callable[[Member, ReplyTask, Permit], Awaitable[PlayedReply]]
    # Must flush the endpoint and return only on its matching stop receipt.
    stop: Callable[[Member, int], Awaitable[None]]
    # Only the unique input's already-closed capture may enter ASR.
    transcribe: Callable[[str], Awaitable[str]]


class CoordinationSession:
    def __init__(
        self,
        *,
        session_id: str,
        input_device_id: str,
        members: tuple[Member, ...],
        ports: Ports,
        discussion: bool = False,
        reply_budget: int = 8,
        stop_timeout: float = 2.0,
        stage_timeout: float = 30.0,
    ):
        if not session_id or not input_device_id or not members:
            raise ValueError("session, input and members required")
        if len(members) > 16 or len({m.companion_id for m in members}) != len(members):
            raise ValueError("unique bounded Companion membership required")
        devices = [m.device_id for m in members]
        if input_device_id in devices or len(set(devices)) != len(devices):
            raise ValueError("input and response devices must be distinct")
        if any(not m.companion_id or not m.device_id for m in members):
            raise ValueError("member identities required")
        if not 1 <= reply_budget <= 32 or stop_timeout <= 0 or stage_timeout <= 0:
            raise ValueError("bounded positive budgets required")
        self.session_id, self.input_device_id = session_id, input_device_id
        self.members, self.ports = members, ports
        self.discussion, self.reply_budget = discussion, reply_budget
        self.stop_timeout, self.stage_timeout = stop_timeout, stage_timeout
        self.epoch = 0
        self.state = "waiting"
        self.member_states = {m.companion_id: "waiting" for m in members}
        self.failures: dict[str, str] = {}
        self.history: list[Message] = []
        self.events: list[dict] = []
        self._captures: set[str] = set()
        self._capture: str | None = None
        self._held = False
        self._closed = False
        self._round: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._stop_tasks: set[asyncio.Task] = set()
        self._active_turn: str | None = None

    def _event(self, kind: str, **facts) -> None:
        # Bounded diagnostic history; never stores captured audio or credentials.
        self.events.append(dict(event=kind, epoch=self.epoch, **facts))
        del self.events[:-512]
        logging.getLogger(__name__).info("coordination session=%s event=%s epoch=%s facts=%s",
            self.session_id, kind, self.epoch, facts)

    def _spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)

        def done(value):
            self._tasks.discard(value)
            if not value.cancelled():
                value.exception()  # All task failures are observed.

        task.add_done_callback(done)
        return task

    def press(self, *, device_id: str, capture_id: str) -> int:
        """Called after the input opens capture locally; never awaits stop/ASR.

        Reliable duplicate press is idempotent. A new press invalidates all old
        work before scheduling parallel stop, even during ASR or a decision.
        """
        if self._closed or device_id != self.input_device_id:
            raise ValueError("input is not authorized for this session")
        if capture_id == self._capture and self._held:
            return self.epoch
        if not capture_id or capture_id in self._captures or len(self._captures) >= 256:
            raise ValueError("invalid/replayed capture or session capture budget exhausted")
        self._captures.add(capture_id)
        self._capture, self._held = capture_id, True
        self.epoch += 1
        self._active_turn = None
        if self._round is not None:
            self._round.cancel()
        self.state = "recording"
        self.member_states = {
            m.companion_id: "failed" if m.companion_id in self.failures else "waiting"
            for m in self.members
        }
        self._event("press", capture_id=capture_id)
        self._dispatch_stop()
        return self.epoch

    def _dispatch_stop(self) -> None:
        task = self._spawn(self._stop_all(self.epoch))
        self._stop_tasks.add(task)
        task.add_done_callback(self._stop_tasks.discard)

    async def _stop_all(self, epoch: int) -> None:
        async def stop(member):
            try:
                async with asyncio.timeout(self.stop_timeout):
                    await self.ports.stop(member, epoch)
            except Exception as exc:
                # A failed endpoint stays quarantined for this session. Recovery
                # requires fresh preparation, not a later unrelated receipt.
                self.failures[member.companion_id] = f"stop:{type(exc).__name__}"
                if epoch == self.epoch:
                    self.member_states[member.companion_id] = "failed"
                self._event("stop_failed", device_id=member.device_id, stop_epoch=epoch)
            else:
                self._event("stopped", device_id=member.device_id, stop_epoch=epoch)

        await asyncio.gather(*(stop(member) for member in self.members))

    def release(self, *, device_id: str, capture_id: str) -> asyncio.Task | None:
        """Input has already stopped local capture; duplicate/old releases ignored."""
        if device_id != self.input_device_id or self._closed:
            raise ValueError("input is not authorized for this session")
        if capture_id != self._capture or not self._held:
            return None
        self._held = False
        self.state = "transcribing"
        self._event("release", capture_id=capture_id)
        self._round = self._spawn(self._run(self.epoch, capture_id, tuple(self._stop_tasks)))
        return self._round

    def _valid(self, epoch: int) -> bool:
        return not self._closed and not self._held and epoch == self.epoch

    def _check(self, epoch: int) -> None:
        if not self._valid(epoch):
            raise asyncio.CancelledError("obsolete round")

    async def _run(self, epoch: int, capture: str, stops: tuple[asyncio.Task, ...]) -> None:
        try:
            async with asyncio.timeout(self.stage_timeout):
                text = await self.ports.transcribe(capture)
            self._check(epoch)
            # Recording and ASR do not wait for stop acknowledgements. Starting
            # new output does; one failed stop cannot authorize further speech.
            if stops:
                await asyncio.shield(asyncio.gather(*stops))
            self._check(epoch)
            if self.failures:
                # An unconfirmed stop may still be audible. Starting a healthy
                # endpoint would violate global serial playback too. Keep input
                # available, but require fresh preparation before any new output.
                raise RuntimeError("not all response devices confirmed silence")
            if not text.strip():
                self.state = "waiting"
                self._event("empty_input")
                return
            trigger = Message(
                message_id=capture, author_kind="user", author_id=self.input_device_id, text=text
            )
            self.history.append(trigger)
            remaining = self.reply_budget
            while remaining:
                self._check(epoch)
                candidates = tuple(
                    Candidate(companion_id=m.companion_id, description=m.description)
                    for m in self.members
                    if m.companion_id not in self.failures
                )
                if not candidates:
                    raise RuntimeError("no healthy response devices")
                self.state = "deciding"
                request = DecisionRequest(
                    decision_id=uuid4().hex,
                    context_ref=self.session_id,
                    context_version=len(self.history),
                    membership_revision=1,
                    cancellation_epoch=epoch,
                    trigger=trigger,
                    context=Context(recent_messages=tuple(self.history[-16:])),
                    candidates=candidates,
                    constraints=Constraints(max_next_speakers=min(remaining, len(candidates))),
                    timeout_ms=min(60000, max(1, int(self.stage_timeout * 1000))),
                )
                async with asyncio.timeout(self.stage_timeout):
                    result = await self.ports.decide(request)
                self._check(epoch)
                validate_proposal(request, result)
                proposal = result.proposal
                if proposal is None or proposal.action in {"wait", "finish"}:
                    break
                self._event("decision", participants=list(proposal.participants))
                for companion_id in proposal.participants:
                    self._check(epoch)
                    if companion_id in self.failures:
                        raise RuntimeError("response device is quarantined")
                    member = next(m for m in self.members if m.companion_id == companion_id)
                    turn_id = uuid4().hex
                    self._active_turn = turn_id
                    permit = Permit(
                        turn_id,
                        epoch,
                        lambda e=epoch, c=companion_id, t=turn_id: (
                            self._valid(e) and self._active_turn == t and c not in self.failures
                        ),
                        lambda phase, c=companion_id: self._phase(c, phase),
                    )
                    self.state = "responding"
                    self.member_states[companion_id] = "thinking"
                    self._event("reply_started", companion_id=companion_id, turn_id=turn_id)
                    # Include preceding public replies in ordered execution without
                    # rerouting or sharing any member's private conversation state.
                    turn_request = ReplyTask(
                        decision_id=request.decision_id,
                        context_ref=self.session_id,
                        trigger=request.trigger,
                        public_context=Context(recent_messages=tuple(self.history[-16:])),
                    )
                    # The reply owns generation and native playout. Their own
                    # failure/cancellation contracts govern completion; the short
                    # ASR/decision RPC deadline must not truncate audible speech.
                    # PTT, close and transport loss still revoke this permit.
                    played = await self.ports.reply(member, turn_request, permit)
                    permit.check()
                    self._active_turn = None
                    if not played.completed:
                        raise RuntimeError("reply did not finish playback")
                    trigger = Message(
                        message_id=turn_id,
                        author_kind="companion",
                        author_id=companion_id,
                        text=played.text,
                    )
                    self.history.append(trigger)
                    remaining -= 1
                    self.member_states[companion_id] = "waiting"
                    self._event("reply_played", companion_id=companion_id)
                # Explicit discussion only; a normal question does not self-loop.
                if not self.discussion or proposal.action == "clarify":
                    break
            self._check(epoch)
            self.state = "waiting"
            self._event("waiting", remaining_budget=remaining)
        except asyncio.CancelledError:
            if self._valid(epoch):
                self._fail_round("cancelled")
            raise
        except Exception as exc:
            if self._valid(epoch):
                self._fail_round(type(exc).__name__)
        finally:
            if epoch == self.epoch:
                for key in self.member_states:
                    if self.member_states[key] in {"thinking", "speaking"}:
                        self.member_states[key] = "waiting"

    def _phase(self, companion_id: str, phase: str) -> None:
        self.member_states[companion_id] = phase
        self._event("reply_phase", companion_id=companion_id, phase=phase)

    def _fail_round(self, reason: str) -> None:
        self.state = "failed"
        self._event("round_failed", reason=reason)
        self.epoch += 1
        self._active_turn = None
        for key in self.member_states:
            if self.member_states[key] in {"thinking", "speaking"}:
                self.member_states[key] = "failed"
        self._dispatch_stop()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed, self._held = True, False
        self.epoch += 1
        self.state = "closed"
        self._active_turn = None
        # Revoke first; remote cancellation cleanup cannot delay stop dispatch.
        for task in tuple(self._tasks):
            task.cancel()
        await self._stop_all(self.epoch)
        pending = tuple(self._tasks)
        if pending:
            await asyncio.wait(pending, timeout=self.stop_timeout)
        self._event("closed")
