"""Admin: gRPC chat test — full pairing + Chat flow via real gRPC."""

from __future__ import annotations

import asyncio
import json
import uuid

import grpc
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from eidolon_agent.transport.grpc.proto import pb, pbg

router = APIRouter()


class ChatTestRequest(BaseModel):
    tenant_id: str = "demo"
    user_id: str = "alice"
    template_id: str = "caretaker_jiezhi"
    text: str = ""


@router.post("/chat/test")
async def chat_test(body: ChatTestRequest, request: Request):
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
    turn_id = uuid.uuid4().hex
    conv_id = f"admin-test-{uuid.uuid4().hex[:8]}"

    async def _stream():
        async with grpc.aio.insecure_channel(target) as channel:
            stub = pbg.EidolonAgentStub(channel)

            # Exchange pairing code -> device token.
            exch = await stub.ExchangePairingCode(
                pb.ExchangeRequest(
                    pairing_code=rec.code,
                    device_id=f"admin-test-{uuid.uuid4().hex[:8]}",
                    device_name="Admin Chat Test",
                )
            )
            metadata = [("authorization", f"Bearer {exch.device_token}")]

            yield _sse("status", {"message": "paired", "user_id": exch.user_id})

            # Open Chat bidi stream.
            async def _requests():
                yield pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id=turn_id,
                        conversation_id=conv_id,
                        text=body.text,
                    )
                )
                await asyncio.sleep(30)

            stream = stub.Chat(_requests(), metadata=metadata)
            async for ev in stream:
                kind = pb.TurnEvent.Kind.Name(ev.kind)
                data = dict(ev.data) if ev.data else {}
                yield _sse("event", {
                    "turn_id": ev.turn_id,
                    "seq": ev.seq,
                    "kind": kind,
                    "data": data,
                })
                if kind in ("DONE", "ERROR"):
                    break

    return StreamingResponse(_stream(), media_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
