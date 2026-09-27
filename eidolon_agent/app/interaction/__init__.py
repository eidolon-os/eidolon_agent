"""Transport-neutral reply preparation within an already authorized runtime.

This is the first single-Companion slice of interaction ingress. Preparation is
not durable acceptance, delivery, or permission to select another Companion.
The transport retains stream scheduling, interruption and signal collection.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from eidolon_agent.core.errors import NotFoundError, PermissionDeniedError
from eidolon_agent.core.types.conversation import validate_conversation_id
from eidolon_agent.core.types.coordination import CoordinatedInput
from eidolon_agent.core.types.presentation import PresentationFeedback
from eidolon_agent.core.types.signal import SignalDigest
from eidolon_agent.core.types.turn import TurnInput, TurnTrigger
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.domain.agent import AgentInstance, CompanionAgent
from eidolon_agent.domain.runtime_session import AuthorizedRuntimeSession


class ReplyRegistry(Protocol):
    async def resolve_runtime(
        self, *, owner_id: str, companion_id: str, genome_id: str
    ) -> AgentInstance: ...


@dataclass(frozen=True, slots=True)
class ReplyRequest:
    """Input facts only: identity and target come from the authorized scope."""

    conversation_id: str
    text: str
    input_modality: str
    turn_id: str = ""
    trace_id: str = ""
    request_id: str = ""
    speculative: bool = False
    realtime: SignalDigest | None = None
    metadata: dict = field(default_factory=dict)
    coordination: CoordinatedInput | None = None


@dataclass(frozen=True, slots=True)
class PreparedReply:
    agent: CompanionAgent
    turn: TurnInput


class AcceptReply:
    def __init__(self, registry: ReplyRegistry) -> None:
        self._registry = registry

    async def prepare(
        self, scope: AuthorizedRuntimeSession, request: ReplyRequest
    ) -> PreparedReply:
        conversation_id = validate_conversation_id(request.conversation_id)
        modality = request.input_modality.strip().lower()
        if modality not in {"voice", "text"}:
            raise ValueError("input_modality must be 'voice' or 'text'")
        runtime = scope.runtime
        instance = await self._registry.resolve_runtime(
            owner_id=scope.owner_id,
            companion_id=scope.companion_id,
            genome_id=runtime.genome_id,
        )
        if (instance.owner_id, instance.companion_id, instance.genome_id) != (
            scope.owner_id,
            scope.companion_id,
            runtime.genome_id,
        ):
            raise PermissionDeniedError("registry returned runtime outside authorized scope")
        if instance.agent is None:
            raise NotFoundError("authorized Companion runtime has no agent")
        metadata = deepcopy(request.metadata)
        if request.speculative:
            metadata["speculative"] = True
        turn = TurnInput(
            turn_id=request.turn_id or uuid4().hex,
            conversation_id=conversation_id,
            session_id=scope.session_id,
            presentation_feedback=PresentationFeedback(),
            context=TurnContext(
                owner_id=scope.owner_id,
                companion_id=scope.companion_id,
                device_id=scope.device_id,
                memory_realm_id=runtime.memory_realm_id,
                genome_id=runtime.genome_id,
                trace_id=request.trace_id or uuid4().hex,
                request_id=request.request_id or uuid4().hex,
                schema_version=runtime.schema_version,
                genome_hash=runtime.genome_hash,
                realizer_version=runtime.realizer_version,
            ),
            input_modality=modality,
            trigger=(
                TurnTrigger.COORDINATED_REPLY
                if request.coordination is not None
                and request.coordination.trigger.author_kind != "user"
                else TurnTrigger.USER_UTTERANCE
            ),
            coordination=request.coordination,
            runtime_config=scope.config,
            text=request.text,
            realtime=request.realtime,
            metadata=metadata,
        )
        return PreparedReply(agent=instance.agent, turn=turn)
