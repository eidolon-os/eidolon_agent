"""Bridge scene replies into the independent role executor and shared presentation.

The caller binds pre-authorized runtime scopes and the existing streaming
presentation facility. This adapter does not resolve devices, mint credentials,
create an LLM/TTS client, or treat model completion as audible completion.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from eidolon_sdk.biz.control.coordination import SceneRole

from eidolon_agent.core.errors import PermissionDeniedError
from eidolon_agent.core.types.turn import TurnEvent, TurnEventKind
from eidolon_agent.domain.runtime_session import AuthorizedRuntimeSession

from . import Member, Permit, PlayedReply, ReplyTask
from .role_reply import RoleMember, RoleReplyExecutor, RoleReplyRequest

Presenter = Callable[[Member, RoleReplyRequest, AsyncIterator[TurnEvent], Permit], Awaitable[bool]]


class CoordinatedReplies:
    def __init__(
        self,
        *,
        context_ref: str,
        input_device_id: str,
        bindings: tuple[tuple[Member, AuthorizedRuntimeSession], ...],
        executor: RoleReplyExecutor,
        present: Presenter,
    ):
        if not context_ref or not input_device_id or not bindings:
            raise ValueError("prepared coordination bindings required")
        if len({scope.owner_id for _, scope in bindings}) != 1:
            raise PermissionDeniedError("mixed Owner runtime bindings")
        self._bindings = {}
        devices = set()
        for member, scope in bindings:
            if (
                member.companion_id != scope.companion_id
                or scope.device_id != input_device_id
                or member.device_id == input_device_id
                or member.device_id in devices
                or member.companion_id in self._bindings
            ):
                raise PermissionDeniedError("inconsistent coordination runtime binding")
            self._bindings[member.companion_id] = (member, scope)
            devices.add(member.device_id)
        self._context_ref = context_ref
        self._input_device_id = input_device_id
        self._executor, self._present = executor, present
        self._roles = tuple(RoleMember(member.companion_id, member.role or SceneRole(
            name=scope.runtime.genome.constitution.name)) for member, scope in bindings)

    async def __call__(self, member: Member, task: ReplyTask, permit: Permit) -> PlayedReply:
        permit.check()
        binding = self._bindings.get(member.companion_id)
        if binding is None or member != binding[0] or task.context_ref != self._context_ref:
            raise PermissionDeniedError("reply is outside prepared coordination bindings")
        scope = binding[1]
        source = task.trigger
        if (
            (source.author_kind == "user" and source.author_id != self._input_device_id)
            or (source.author_kind == "companion" and source.author_id not in self._bindings)
            or source.author_kind == "system"
        ):
            raise PermissionDeniedError("unrecognized coordination source")
        prepared = RoleReplyRequest(
            scope=scope, context_ref=self._context_ref, turn_id=permit.turn_id,
            assignment_revision=1, members=self._roles,
            trigger=source, public_context=task.public_context,
        )
        permit.check()
        fragments = []
        completed = False
        exhausted = False

        async def events():
            nonlocal completed, exhausted
            stream = self._executor.run(prepared)
            try:
                async for event in stream:
                    permit.check()
                    if event.turn_id != permit.turn_id:
                        raise ValueError("foreign turn event")
                    if event.kind is TurnEventKind.ERROR:
                        raise RuntimeError("Companion reply failed")
                    if (
                        event.kind is TurnEventKind.DELTA
                        and event.data.get("role", "answer") == "answer"
                    ):
                        fragments.append(event.data.get("text", ""))
                    if event.kind is TurnEventKind.DONE:
                        completed = event.data.get("status") == "ok"
                        if not completed:
                            raise RuntimeError("Companion reply did not complete successfully")
                    yield event
                exhausted = True
            finally:
                await stream.aclose()

        stream = events()
        try:
            played = await self._present(member, prepared, stream, permit)
            permit.check()
            return PlayedReply("".join(fragments), played is True and completed and exhausted)
        finally:
            await stream.aclose()
