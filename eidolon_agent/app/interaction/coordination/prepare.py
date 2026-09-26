"""Prepare the IP role-group executor from an authorized group selection.

Solo conversations continue through their existing entrypoint and input mode.
The caller must authenticate the Owner and validate every current DeviceRef and
PTT/output capability before invoking this use case. This is not a public API:
selection fields alone cannot grant device access. Companion authorization uses
the same runtime authority as ordinary replies, before any output is permitted.
"""

from collections.abc import Awaitable, Callable

from eidolon_sdk.biz.control.coordination import CoordinationSelection
from eidolon_sdk.biz.participation import DecisionRequest, DecisionResult

from eidolon_agent.app.interaction import AcceptReply
from eidolon_agent.core.errors import PermissionDeniedError
from eidolon_agent.domain.runtime_session import RuntimeSessionAuthorizer

from . import CoordinationSession, Member, Ports
from .replies import CoordinatedReplies, Presenter


async def prepare_session(
    selection: CoordinationSelection,
    *,
    authenticated_owner_id: str,
    runtime_sessions: RuntimeSessionAuthorizer,
    accept: AcceptReply,
    present: Presenter,
    decide: Callable[[DecisionRequest], Awaitable[DecisionResult]],
    stop: Callable[[Member, int], Awaitable[None]],
    transcribe: Callable[[str], Awaitable[str]],
) -> CoordinationSession:
    if not authenticated_owner_id or not authenticated_owner_id.strip():
        raise PermissionDeniedError("authenticated Owner is required")
    input_id = selection.input_device.device_instance_id
    bindings = []
    for selected in selection.members:
        scope = await runtime_sessions.authorize(
            owner_id=authenticated_owner_id,
            companion_id=selected.companion_id,
            device_id=input_id,
            session_id=selection.session_id,
        )
        member = Member(selected.companion_id, selected.output_device.device_instance_id)
        bindings.append((member, scope))
    # No device preparation, model turn or output occurs during the loop above.
    # One refused Companion rejects the whole configuration, not a silent subset.
    reply = CoordinatedReplies(
        context_ref=selection.session_id,
        input_device_id=input_id,
        bindings=tuple(bindings),
        accept=accept,
        present=present,
    )
    return CoordinationSession(
        session_id=selection.session_id,
        input_device_id=input_id,
        members=tuple(member for member, _ in bindings),
        ports=Ports(decide=decide, reply=reply, stop=stop, transcribe=transcribe),
        discussion=selection.discussion,
        reply_budget=selection.reply_budget,
    )
