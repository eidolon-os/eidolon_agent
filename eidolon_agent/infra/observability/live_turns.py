"""Turns that are happening right now.

The Agent owns a turn: it assigns the stages, times them, and writes the one
durable row that says what happened (``record_turn``). That row is written
**when the turn ends**, which is why nothing on this Host could ever draw a
conversation while it was still going on — every consumer downstream was built
for a running turn (a stage whose status is ``running``, a route hop that is
current, a companion that is lit) and no producer ever emitted one.

This board closes that gap at the owner rather than beside it:

* **it observes, it does not decide.** Every entry is derived from the event
  stream the turn already emits to its caller — a turn's own ``STATE``,
  ``TOOL_CALL``, ``TOOL_RESULT``, ``DELTA``, ``DONE``. Nothing here starts,
  stops, or times a turn, and nothing here is asked before a turn acts;
* **it is not storage.** A live turn is interesting for the seconds it runs;
  the durable answer is the turn row, which already exists. So this is an
  in-process, bounded, TTL-swept dict — no schema, no migration, and an Agent
  that restarts simply has nothing in flight, which is true;
* **it cannot affect a turn.** Every write is wrapped by the caller
  (:meth:`observe`) so a telemetry bug degrades the map, never the
  conversation. This is the same rule Channel's own turn telemetry holds.

What it deliberately does **not** do is invent the facts it cannot see. The
engine's internal boundaries (guard, triage, compile) are locals inside one
method and never reach the stream, so this board says nothing about them —
their absence reads downstream as ``pending``, which is what "not known" means
there. Only ``first_delta`` is genuinely observable from outside, and it is the
one that matters: it is the moment the answer starts.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from eidolon_agent.core.types.turn import TurnEvent, TurnEventKind, TurnInput

logger = logging.getLogger("agent.observability.live_turns")

#: How many in-flight turns one Agent process will hold. A turn is one
#: conversation's current utterance, so this is far above any real concurrency;
#: it exists so a leak cannot grow without bound.
_MAX_TURNS = 64

#: How long a turn stays readable after it ends. The durable row is written by
#: a **background** task the engine schedules in its ``finally`` — "post-turn
#: work that runs after DONE was yielded" — so the stream closes before that row
#: exists. Dropping the entry at close leaves a window in which the turn is in
#: neither place, and a map sampling it then shows the conversation blink out of
#: existence. So a finished turn is handed over rather than deleted: it keeps
#: being served, with the final status its own DONE event carried, until the
#: written row takes over (the reader prefers that one) or this expires.
_HANDOVER_SECONDS = 10.0

#: A turn nobody has said anything about for this long is not running any more,
#: whatever the reason (the stream was abandoned, the process was paused, a bug
#: skipped the ``finally``). Reporting it as live forever is the failure mode
#: this sweep exists to prevent: it is exactly how "currently writing memory"
#: ended up on the map for turns that had finished minutes earlier.
_TTL_SECONDS = 180.0

#: The status a live turn reports. Chosen because every consumer downstream
#: already understands it — the stage projection, the activity projection and
#: the app's own "is this stage happening" all key on this word.
RUNNING = "running"


@dataclass(frozen=True, slots=True)
class LiveToolUse:
    """What the turn has asked of its tools so far."""

    count: int
    completed: int
    error_count: int
    names: tuple[str, ...]

    @property
    def running(self) -> bool:
        return self.count > self.completed


@dataclass(frozen=True, slots=True)
class LiveTurnView:
    """One turn, as it stands at the instant it was read."""

    turn_id: str
    conversation_id: str
    owner_id: str
    companion_id: str
    device_id: str | None
    memory_realm_id: str | None
    genome_id: str | None
    genome_hash: str | None
    trace_id: str | None
    runtime_session_id: str | None
    trigger: str
    input_modality: str | None
    started_at: datetime
    #: ``running`` until this turn's own DONE event says otherwise. A settled
    #: entry keeps being served through the hand-over window above, and saying
    #: ``running`` then would be the one lie this board could tell.
    status: str
    finished_at: datetime | None
    latency_first_delta_ms: int | None
    tools: LiveToolUse


@dataclass(slots=True)
class _Entry:
    view: LiveTurnView
    touched_at: float
    started_monotonic: float
    #: When this turn ended, if it has. Set from its DONE event, which is also
    #: what starts the hand-over clock.
    settled_at: float | None = None
    tool_names: list[str] = field(default_factory=list)
    tool_calls: int = 0
    tool_results: int = 0
    tool_errors: int = 0


class LiveTurnBoard:
    """The turns this Agent process is running, right now."""

    def __init__(
        self,
        *,
        max_turns: int = _MAX_TURNS,
        ttl_seconds: float = _TTL_SECONDS,
        handover_seconds: float = _HANDOVER_SECONDS,
        monotonic: Any = time.monotonic,
        wall_clock: Any = None,
    ) -> None:
        self._entries: dict[str, _Entry] = {}
        self._max_turns = max_turns
        self._ttl_seconds = ttl_seconds
        self._handover_seconds = handover_seconds
        self._monotonic = monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))

    # ── the observation point ──────────────────────────────────────────────

    async def observe(
        self, turn_input: TurnInput, stream: AsyncIterator[TurnEvent]
    ) -> AsyncIterator[TurnEvent]:
        """Yield ``stream`` unchanged, keeping this board current as it goes.

        The stream's semantics are the contract here: every event passes through
        untouched and in order, an exception propagates, and abandoning the
        iterator still ends the entry — which is why the removal lives in
        ``finally`` rather than after the loop. Nothing this board does is
        awaited on the turn's behalf.
        """

        self._guard(lambda: self._begin(turn_input))
        try:
            async for event in stream:
                self._guard(lambda event=event: self._saw(event))
                yield event
        finally:
            self._guard(lambda: self._end(turn_input.turn_id))

    def _guard(self, call: Any) -> None:
        """Telemetry cannot break a conversation. It can only miss one."""

        try:
            call()
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.exception("live turn board failed; the turn is unaffected")

    # ── reading ────────────────────────────────────────────────────────────

    def snapshot(
        self, *, owner_id: str | None = None, companion_id: str | None = None
    ) -> list[LiveTurnView]:
        """The turns in flight, newest first, for whoever is asking.

        Filters mirror the durable read's, so a caller applying an owner or a
        Companion gets the same scoping from both halves of one answer.
        """

        self._sweep()
        views = [
            entry.view
            for entry in self._entries.values()
            if (owner_id is None or entry.view.owner_id == owner_id)
            and (companion_id is None or entry.view.companion_id == companion_id)
        ]
        return sorted(views, key=lambda view: view.started_at, reverse=True)

    # ── derivation ─────────────────────────────────────────────────────────

    def _begin(self, turn_input: TurnInput) -> None:
        self._sweep()
        if len(self._entries) >= self._max_turns and turn_input.turn_id not in self._entries:
            # Drop the oldest rather than refuse the newest: the interesting
            # turn is the one starting now.
            oldest = min(self._entries.values(), key=lambda entry: entry.touched_at)
            self._entries.pop(oldest.view.turn_id, None)
        # Read directly, never with a default: a ``TurnContext`` that does not
        # carry these is a shape this board does not understand, and failing
        # here is how that gets found. Defaulting is how a whole map once came
        # out nameless and unbound while the authority was reporting the truth.
        context = turn_input.context
        now = self._monotonic()
        self._entries[turn_input.turn_id] = _Entry(
            view=LiveTurnView(
                turn_id=turn_input.turn_id,
                conversation_id=turn_input.conversation_id,
                owner_id=context.owner_id,
                companion_id=context.companion_id,
                device_id=_optional(context.device_id),
                memory_realm_id=_optional(context.memory_realm_id),
                genome_id=_optional(context.genome_id),
                genome_hash=_optional(context.genome_hash),
                trace_id=_optional(context.trace_id),
                runtime_session_id=_optional(turn_input.session_id),
                trigger=_enum_value(turn_input.trigger),
                input_modality=_enum_value(turn_input.input_modality) or None,
                started_at=self._wall_clock(),
                status=RUNNING,
                finished_at=None,
                latency_first_delta_ms=None,
                tools=_no_tools(),
            ),
            touched_at=now,
            started_monotonic=now,
        )

    def _saw(self, event: TurnEvent) -> None:
        entry = self._entries.get(event.turn_id)
        if entry is None:
            return
        entry.touched_at = self._monotonic()
        if event.kind in (TurnEventKind.DONE, TurnEventKind.ERROR):
            # The turn said how it went. That is the only place this board ever
            # learns a final status, and it is why the hand-over row is truthful
            # rather than a stale "running".
            status = str((event.data or {}).get("status") or "")
            if event.kind is TurnEventKind.ERROR and not status:
                status = "errored"
            entry.settled_at = entry.touched_at
            entry.view = _with(
                entry.view,
                status=status or "unknown",
                finished_at=self._wall_clock(),
            )
            return
        if event.kind is TurnEventKind.DELTA:
            if entry.view.latency_first_delta_ms is None:
                elapsed = int((entry.touched_at - entry.started_monotonic) * 1000)
                entry.view = _with(entry.view, latency_first_delta_ms=max(0, elapsed))
            return
        if event.kind is TurnEventKind.TOOL_CALL:
            entry.tool_calls += 1
            name = _text((event.data or {}).get("name"))
            if name and name not in entry.tool_names:
                entry.tool_names.append(name)
        elif event.kind is TurnEventKind.TOOL_RESULT:
            entry.tool_results += 1
            if (event.data or {}).get("ok") is False:
                entry.tool_errors += 1
        else:
            return
        entry.view = _with(
            entry.view,
            tools=LiveToolUse(
                count=entry.tool_calls,
                completed=entry.tool_results,
                error_count=entry.tool_errors,
                names=tuple(entry.tool_names),
            ),
        )

    def _end(self, turn_id: str) -> None:
        entry = self._entries.get(turn_id)
        if entry is not None and entry.settled_at is not None:
            # Ended and said so: hand over to the row being written, rather than
            # leaving a gap where this turn is nowhere at all.
            return
        # No DONE reached this board, so it does not know how the turn went — the
        # consumer hung up, or the process is going down. Keeping the entry would
        # report it as still running, which is worse than not reporting it: the
        # engine persists a cancelled row on this path and that row is the answer.
        self._entries.pop(turn_id, None)

    def _sweep(self) -> None:
        if not self._entries:
            return
        now = self._monotonic()
        for turn_id, entry in list(self._entries.items()):
            if entry.settled_at is not None:
                if now - entry.settled_at >= self._handover_seconds:
                    self._entries.pop(turn_id, None)
                continue
            if entry.touched_at < now - self._ttl_seconds:
                logger.warning("live turn %s expired without ending; dropping", turn_id)
                self._entries.pop(turn_id, None)


def _no_tools() -> LiveToolUse:
    return LiveToolUse(count=0, completed=0, error_count=0, names=())


def _with(view: LiveTurnView, **changes: Any) -> LiveTurnView:
    from dataclasses import replace

    return replace(view, **changes)


def _text(value: Any) -> str:
    return str(value) if value is not None else ""


def _optional(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _enum_value(value: Any) -> str:
    return _text(getattr(value, "value", value))


__all__ = ["RUNNING", "LiveToolUse", "LiveTurnBoard", "LiveTurnView"]
