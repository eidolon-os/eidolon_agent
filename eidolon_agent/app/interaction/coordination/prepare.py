"""Prepare the IP role-group executor from an authorized group selection.

Solo conversations continue through their existing entrypoint and input mode.
The caller must authenticate the Owner and validate every current DeviceRef and
PTT/output capability before invoking this use case. This is not a public API:
selection fields alone cannot grant device access. Companion authorization uses
the same runtime authority as ordinary replies, before any output is permitted.
"""

import json
from collections.abc import Awaitable, Callable

from eidolon_sdk.biz.control.coordination import CoordinationSelection
from eidolon_sdk.biz.participation import DecisionRequest, DecisionResult

from eidolon_agent.core.errors import PermissionDeniedError
from eidolon_agent.domain.runtime_session import RuntimeSessionAuthorizer

from . import CoordinationSession, Member, Ports
from .replies import CoordinatedReplies, Presenter
from .role_reply import RoleReplyExecutor


async def prepare_session(
    selection: CoordinationSelection,
    *,
    authenticated_owner_id: str,
    runtime_sessions: RuntimeSessionAuthorizer,
    executor: RoleReplyExecutor,
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
        genome = scope.runtime.genome
        # Shared member identity comes from the authorized Companion snapshot,
        # never a second persona maintained by a saved Team. Relationship and
        # private memory fields do not enter the public participation request.
        profile = {
            "name": genome.constitution.name,
            "self_concept": genome.constitution.self_concept,
            "portrait": genome.character.portrait,
        }
        description = json.dumps(profile, ensure_ascii=False)
        if len(description) > 4096:
            # The participation contract has a bounded public summary. Full
            # authorized persona remains available to its own reply realizer.
            profile["self_concept"] = profile["self_concept"][:1000]
            profile["portrait"] = profile["portrait"][:2000]
            description = json.dumps(profile, ensure_ascii=False)
        member = Member(selected.companion_id, selected.output_device.device_instance_id,
                        description=description, display_name=genome.constitution.name)
        bindings.append((member, scope))
    # No device preparation, model turn or output occurs during the loop above.
    # One refused Companion rejects the whole configuration, not a silent subset.
    reply = CoordinatedReplies(
        context_ref=selection.session_id,
        input_device_id=input_id,
        bindings=tuple(bindings),
        executor=executor,
        present=present,
    )
    return CoordinationSession(
        session_id=selection.session_id,
        input_device_id=input_id,
        members=tuple(member for member, _ in bindings),
        ports=Ports(decide=decide, reply=reply, stop=stop, transcribe=transcribe),
        goal=selection.goal,
        reply_budget=selection.reply_budget,
    )
