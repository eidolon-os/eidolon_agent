"""IP-team composition boundary, independent of its WebSocket transport.

Only narrow model and runtime-authority capabilities enter this application.
It does not receive the companionship registry, private memory or tool runtime.
The injected decision port is the only replaceable inference boundary.
There is no fixed-order policy or fallback inside the application.
"""

from dataclasses import dataclass

from eidolon_sdk.biz.control.coordination import CoordinationSelection

from eidolon_agent.core.ports.participation import ParticipationDecisionPort
from eidolon_agent.domain.runtime_session import RuntimeSessionAuthorizer

from . import CoordinationSession
from .prepare import prepare_session
from .role_reply import RoleReplyExecutor


@dataclass(frozen=True)
class PreparedTeam:
    session: CoordinationSession
    policy_version: str


class IpTeamApplication:
    def __init__(self, *, llm, runtime_authority, decide: ParticipationDecisionPort | None):
        self._executor = RoleReplyExecutor(llm)
        self._runtime_sessions = RuntimeSessionAuthorizer(runtime_authority)
        self._decide = decide

    async def prepare(
        self, selection: CoordinationSelection, *, authenticated_owner_id: str,
        present, stop, transcribe,
    ) -> PreparedTeam:
        if self._decide is None:
            raise ValueError("TEAM_DECISION_NOT_CONFIGURED")
        session = await prepare_session(
            selection,
            authenticated_owner_id=authenticated_owner_id,
            runtime_sessions=self._runtime_sessions,
            executor=self._executor,
            present=present,
            decide=self._decide,
            stop=stop,
            transcribe=transcribe,
        )
        return PreparedTeam(session, "semantic-step-v2")
