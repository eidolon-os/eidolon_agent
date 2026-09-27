"""IP-team composition boundary, independent of its WebSocket transport.

Only narrow model and runtime-authority capabilities enter this application.
It does not receive the companionship registry, private memory or tool runtime.
The explicit demo entrypoint remains distinct from a future semantic policy;
model failure must never silently select this policy as a fallback.
"""

from dataclasses import dataclass

from eidolon_sdk.biz.control.coordination import CoordinationSelection

from eidolon_agent.domain.runtime_session import RuntimeSessionAuthorizer

from . import CoordinationSession
from .mock_decision import MockDecision
from .prepare import prepare_session
from .role_reply import RoleReplyExecutor


@dataclass(frozen=True)
class PreparedTeam:
    session: CoordinationSession
    policy_version: str


class IpTeamApplication:
    def __init__(self, *, llm, runtime_authority):
        self._executor = RoleReplyExecutor(llm)
        self._runtime_sessions = RuntimeSessionAuthorizer(runtime_authority)

    async def prepare_demo(
        self, selection: CoordinationSelection, *, authenticated_owner_id: str,
        order: tuple[str, ...], present, stop, transcribe,
    ) -> PreparedTeam:
        # Validate at the application boundary too: non-WebSocket callers must
        # not be able to configure foreign candidates through a demo order.
        if not set(order) <= {member.companion_id for member in selection.members}:
            raise ValueError("demo decision candidate is not a scene member")
        decision = MockDecision(order)
        session = await prepare_session(
            selection,
            authenticated_owner_id=authenticated_owner_id,
            runtime_sessions=self._runtime_sessions,
            executor=self._executor,
            present=present,
            decide=decision,
            stop=stop,
            transcribe=transcribe,
        )
        return PreparedTeam(session, decision.policy_version)
