"""NATS publishers for eidolon-memory writes (turns and KG commands)."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Literal

from eidolon_memory_contracts import (
    ConversationTurnPayload,
    MemoryIntent,
    MemoryIntentCommand,
    conversation_turn_subject,
    envelope_memory_payload,
    memory_command_subject,
)

from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.turn_context import (
    build_memory_actor_context,
    build_memory_space_id,
)
from eidolon_agent.infra.memory.discovery import MemoryRoutingTable

_log = logging.getLogger(__name__)


class MemoryNatsPublisher:
    def __init__(self, *, event_bus, routes: MemoryRoutingTable | None = None) -> None:
        self._bus = event_bus
        self._routes = routes

    async def publish_turn(
        self,
        *,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        device_id: str | None,
        session_id: str | None,
        turn_id: str,
        owner_text: str,
        assistant_text: str,
        metadata: dict | None = None,
    ) -> None:
        context = build_memory_actor_context(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            device_id=device_id,
            session_id=session_id,
        )
        payload = ConversationTurnPayload(
            turn_id=turn_id,
            context=context,
            timestamp=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            user_text=owner_text,
            assistant_text=assistant_text,
            metadata=metadata or {"source": "eidolon-agent"},
        )
        subject = (
            await self._routes.render_turn_subject(context.memory_space_id)
            if self._routes is not None
            else conversation_turn_subject(context.memory_space_id)
        )
        await self._bus.publish(
            Event(
                subject=subject,
                payload=envelope_memory_payload(payload, trace_id=turn_id).model_dump(
                    mode="json"
                ),
                source="memory.nats_pub",
                metadata={"msg_id": turn_id},
            ),
            persistent=True,
        )

    async def publish_structured_intent(
        self,
        *,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        subject: str,
        predicate: str,
        object_: str,
        source_event_id: str,
        tool_call_id: str,
        confidence: float = 0.9,
    ) -> str:
        return await self._publish_structured_lifecycle(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            subject=subject,
            predicate=predicate,
            object_=object_,
            source_event_id=source_event_id,
            tool_call_id=tool_call_id,
            confidence=confidence,
            operation="confirm",
        )

    async def publish_structured_invalidation(
        self,
        *,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        subject: str,
        predicate: str,
        object_: str,
        source_event_id: str,
        tool_call_id: str,
        confidence: float = 0.99,
    ) -> str:
        return await self._publish_structured_lifecycle(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            subject=subject,
            predicate=predicate,
            object_=object_,
            source_event_id=source_event_id,
            tool_call_id=tool_call_id,
            confidence=confidence,
            operation="invalidate",
        )

    async def publish_structured_reactivation(
        self,
        *,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        subject: str,
        predicate: str,
        object_: str,
        source_event_id: str,
        tool_call_id: str,
        confidence: float = 0.99,
    ) -> str:
        return await self._publish_structured_lifecycle(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            subject=subject,
            predicate=predicate,
            object_=object_,
            source_event_id=source_event_id,
            tool_call_id=tool_call_id,
            confidence=confidence,
            operation="reactivate",
        )

    async def _publish_structured_lifecycle(
        self,
        *,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        subject: str,
        predicate: str,
        object_: str,
        source_event_id: str,
        tool_call_id: str,
        confidence: float,
        operation: Literal["confirm", "invalidate", "reactivate"],
    ) -> str:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        memory_space_id = build_memory_space_id(memory_realm_id=memory_realm_id)
        intent_type, wing, memory_type = _structured_intent_classification(predicate)
        identity_claim = f"{subject}\x1f{predicate}\x1f{object_}"
        if operation != "confirm":
            identity_claim = f"{identity_claim}\x1f{operation}"
        intent_id = _explicit_intent_id(
            memory_space_id,
            source_event_id,
            tool_call_id,
            identity_claim,
        )
        request_id = intent_id.removeprefix("intent:")
        intent = MemoryIntent(
            intent_id=intent_id,
            memory_space_id=memory_space_id,
            source_event_id=source_event_id,
            authority="explicit_user",
            intent_type="correction" if operation == "invalidate" else intent_type,
            raw_claim=f"{subject} {predicate} {object_}",
            operation_hint={
                "confirm": "confirm",
                "invalidate": "invalidate",
                "reactivate": "update",
            }[operation],
            subject=subject,
            predicate=predicate,
            object=object_,
            occurred_at=now,
            tool_call_id=tool_call_id,
            confidence=confidence,
            attributes={
                "wing": wing,
                "memory_type": memory_type,
                "importance": 5,
                "tags": ["memory_assert_fact", "structured", operation],
                "source_instance_id": companion_id or "",
            },
        )
        payload = MemoryIntentCommand(
            request_id=request_id,
            memory_space_id=memory_space_id,
            issued_at=now,
            issuer="agent",
            intent=intent,
        )
        return await self._publish_intent(
            payload,
            memory_space_id=memory_space_id,
            source_event_id=source_event_id,
        )

    async def publish_verbatim_intent(
        self,
        *,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        device_id: str | None,
        session_id: str | None,
        text: str,
        source_event_id: str,
        tool_call_id: str,
        confidence: float = 0.99,
        tags: list[str] | None = None,
    ) -> str:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        memory_space_id = build_memory_space_id(memory_realm_id=memory_realm_id)
        intent_id = _explicit_intent_id(
            memory_space_id,
            source_event_id,
            tool_call_id,
            text,
        )
        request_id = intent_id.removeprefix("intent:")
        intent = MemoryIntent(
            intent_id=intent_id,
            memory_space_id=memory_space_id,
            source_event_id=source_event_id,
            authority="explicit_user",
            intent_type="fact",
            raw_claim=text,
            operation_hint="confirm",
            occurred_at=now,
            tool_call_id=tool_call_id,
            confidence=confidence,
            attributes={
                "wing": "Wing_Profile",
                "memory_type": "profile",
                "importance": 5,
                "tags": list(tags or []),
                "scope": "persona",
                "visibility": "all_devices",
                "source_device_id": device_id or "",
                "source_instance_id": companion_id or "",
                "session_id": session_id or "",
            },
        )
        payload = MemoryIntentCommand(
            request_id=request_id,
            memory_space_id=memory_space_id,
            issued_at=now,
            issuer="agent",
            intent=intent,
        )
        return await self._publish_intent(
            payload,
            memory_space_id=memory_space_id,
            source_event_id=source_event_id,
        )

    async def publish_commitment_intent(
        self,
        *,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        promisor: str,
        predicate: Literal["promised", "committed_to", "planned_to"],
        action: str,
        raw_claim: str,
        source_event_id: str,
        tool_call_id: str,
        operation: Literal["add", "update", "invalidate", "confirm"] = "confirm",
        target_id: str | None = None,
        beneficiaries: list[str] | None = None,
        participants: list[str] | None = None,
        condition: str | None = None,
        due_at: str | None = None,
        status: Literal[
            "proposed", "confirmed", "fulfilled", "cancelled", "superseded"
        ]
        | None = None,
        confidence: float = 0.99,
    ) -> str:
        """Publish one explicit Commitment lifecycle intent over the command bus."""
        if predicate not in {"promised", "committed_to", "planned_to"}:
            raise ValueError("unsupported commitment predicate")
        if target_id is None and operation not in {"add", "confirm"}:
            raise ValueError("commitment update requires target_id")
        memory_space_id = build_memory_space_id(memory_realm_id=memory_realm_id)
        attributes: dict[str, object] = {}
        if beneficiaries is not None:
            attributes["beneficiaries"] = beneficiaries
        if participants is not None:
            attributes["participants"] = participants
        if condition is not None:
            attributes["condition"] = condition
        if due_at is not None:
            attributes["due_at"] = due_at
        if status is not None:
            attributes["status"] = status
        identity_payload = json.dumps(
            {
                "target_id": target_id,
                "promisor": promisor,
                "predicate": predicate,
                "action": action,
                "operation": operation,
                "raw_claim": raw_claim,
                "attributes": attributes,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        intent_id = _explicit_intent_id(
            memory_space_id,
            source_event_id,
            tool_call_id,
            identity_payload,
        )
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = MemoryIntentCommand(
            request_id=intent_id.removeprefix("intent:"),
            memory_space_id=memory_space_id,
            issued_at=now,
            issuer="agent",
            intent=MemoryIntent(
                intent_id=intent_id,
                memory_space_id=memory_space_id,
                source_event_id=source_event_id,
                authority="explicit_user",
                intent_type="commitment",
                raw_claim=raw_claim,
                operation_hint=operation,
                target_id=target_id,
                subject=promisor,
                predicate=predicate,
                object=action,
                occurred_at=now,
                tool_call_id=tool_call_id,
                confidence=confidence,
                attributes=attributes,
            ),
        )
        return await self._publish_intent(
            payload,
            memory_space_id=memory_space_id,
            source_event_id=source_event_id,
        )

    async def _publish_intent(
        self,
        payload: MemoryIntentCommand,
        *,
        memory_space_id: str,
        source_event_id: str,
    ) -> str:
        nats_subject = (
            await self._routes.render_cmd_subject(memory_space_id)
            if self._routes is not None
            else memory_command_subject(memory_space_id)
        )
        await self._bus.publish(
            Event(
                subject=nats_subject,
                payload=envelope_memory_payload(
                    payload, trace_id=source_event_id
                ).model_dump(mode="json"),
                source="memory.nats_pub",
                metadata={"msg_id": payload.request_id},
            ),
            persistent=True,
        )
        return payload.request_id


def _explicit_intent_id(
    memory_space_id: str,
    source_event_id: str,
    tool_call_id: str,
    claim: str,
) -> str:
    raw = "\x1f".join((memory_space_id, source_event_id, tool_call_id, claim))
    return f"intent:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _structured_intent_classification(predicate: str) -> tuple[str, str, str]:
    if predicate in {"likes", "dislikes", "prefers"}:
        return "preference", "Wing_Life", "preference"
    if predicate in {"promised", "committed_to", "planned_to"}:
        return "commitment", "Wing_Future", "commitment"
    if predicate in {"attended", "experienced", "achieved"}:
        return "episode", "Wing_Life", "event"
    return "fact", "Wing_Profile", "profile"
