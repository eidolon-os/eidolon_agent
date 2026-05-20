"""Admin: gRPC chat test — full pairing + ChatOnce flow via real gRPC."""

from __future__ import annotations

import logging
import uuid

import grpc
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from eidolon_agent.transport.grpc.proto import pb, pbg

_log = logging.getLogger(__name__)

router = APIRouter()


class ChatTestRequest(BaseModel):
    tenant_id: str = "demo"
    user_id: str = "alice"
    template_id: str = "caretaker_jiezhi"
    text: str = ""


class ChatTestResponse(BaseModel):
    turn_id: str
    assistant_text: str
    triage: str
    latency_first_delta_ms: int
    user_id: str


@router.post("/chat/test", response_model=ChatTestResponse)
async def chat_test(body: ChatTestRequest, request: Request):
    """Full gRPC round-trip: pairing → auth → ChatOnce → response.

    Exercises the same path as a real LiveKit / device caller.
    """
    settings = request.app.state.settings
    pairing = request.app.state.pairing
    registry = request.app.state.agent_registry

    # Ensure agent instance exists.
    from eidolon_agent.core.errors import ConflictError

    try:
        await registry.start_instance(
            template_id=body.template_id,
            tenant_id=body.tenant_id,
            user_id=body.user_id,
        )
    except ConflictError:
        pass

    # Issue pairing code.
    rec = await pairing.issue_code(
        tenant_id=body.tenant_id,
        user_id=body.user_id,
        default_template_id=body.template_id,
        issued_by_actor="admin-chat-test",
    )

    target = f"{settings.grpc.tcp_host}:{settings.grpc.tcp_port}"

    try:
        async with grpc.aio.insecure_channel(target) as channel:
            stub = pbg.EidolonAgentStub(channel)

            # Exchange pairing code → device token.
            exch = await stub.ExchangePairingCode(
                pb.ExchangeRequest(
                    pairing_code=rec.code,
                    device_id=f"admin-test-{uuid.uuid4().hex[:8]}",
                    device_name="Admin Chat Test",
                )
            )
            metadata = [("authorization", f"Bearer {exch.device_token}")]

            # ChatOnce — unary RPC, same auth + TurnEngine path.
            resp = await stub.ChatOnce(
                pb.ChatOnceRequest(
                    turn_id=uuid.uuid4().hex,
                    conversation_id=f"admin-test-{uuid.uuid4().hex[:8]}",
                    text=body.text,
                ),
                metadata=metadata,
            )

        return ChatTestResponse(
            turn_id=resp.turn_id,
            assistant_text=resp.assistant_text,
            triage=resp.triage,
            latency_first_delta_ms=resp.latency_first_delta_ms,
            user_id=exch.user_id,
        )

    except grpc.aio.AioRpcError as exc:
        _log.error("chat test gRPC error: %s", exc.details())
        raise HTTPException(status_code=502, detail=exc.details()) from exc
