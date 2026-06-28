"""Admin: gRPC chat test — full pairing + Chat bidi stream via real gRPC."""

from __future__ import annotations

import logging
import uuid

import grpc
from eidolon_sdk.core.streaming import encode_sse_event
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from eidolon_agent.app.transport.grpc.codec import struct_to_dict
from eidolon_agent.app.transport.grpc.proto import pb, pbg

_log = logging.getLogger(__name__)

router = APIRouter()


class ChatTestRequest(BaseModel):
    owner_id: str = "demo"
    companion_id: str = "companion-demo"
    text: str = ""


@router.post("/chat/test")
async def chat_test(body: ChatTestRequest, request: Request):
    """Stream a turn over the real gRPC Chat bidi path, exposed as SSE.

    Same code path as a LiveKit caller: pairing → ExchangePairingCode → Chat.
    """
    settings = request.app.state.settings
    pairing = request.app.state.pairing
    data_store = getattr(request.app.state, "data_store", None)
    if data_store is None:
        raise RuntimeError("data_store not configured")
    companion = await data_store.companions.get(body.companion_id)
    if companion is None or companion.owner_id != body.owner_id:
        raise RuntimeError("companion not found for owner")
    if not companion.default_memory_realm_id or not companion.current_genome_id:
        raise RuntimeError("companion has no default memory realm or current genome")

    # The registry creates the agent lazily on the first Chat RPC; nothing
    # to do here besides issuing the pairing code.

    rec = await pairing.issue_code(
        owner_id=body.owner_id,
        companion_id=body.companion_id,
        memory_realm_id=companion.default_memory_realm_id,
        genome_id=companion.current_genome_id,
        issued_by_actor="admin-chat-test",
    )
    target = f"{settings.grpc.tcp_host}:{settings.grpc.tcp_port}"

    async def _stream():
        channel = grpc.aio.insecure_channel(target)
        try:
            stub = pbg.EidolonAgentStub(channel)
            exch = await stub.ExchangePairingCode(
                pb.ExchangeRequest(
                    pairing_code=rec.code,
                    device_id=f"admin-test-{uuid.uuid4().hex[:8]}",
                    device_name="Admin Chat Test",
                )
            )
            yield _sse("status", {"message": "paired", "owner_id": exch.owner_id})

            async def _requests():
                yield pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id=uuid.uuid4().hex,
                        conversation_id=f"admin-test-{uuid.uuid4().hex[:8]}",
                        text=body.text,
                    )
                )

            stream = stub.Chat(
                _requests(),
                metadata=(("authorization", f"Bearer {exch.device_token}"),),
            )
            async for ev in stream:
                kind = pb.TurnEvent.Kind.Name(ev.kind)
                data = struct_to_dict(ev.data) if ev.data else {}
                yield _sse("event", {
                    "turn_id": ev.turn_id,
                    "seq": ev.seq,
                    "kind": kind,
                    "data": data,
                })
                if kind in ("DONE", "ERROR"):
                    break
        except grpc.aio.AioRpcError as exc:
            _log.warning("chat test gRPC error: %s", exc.details())
            yield _sse("event", {"kind": "ERROR", "data": {"message": exc.details()}})
        finally:
            await channel.close()

    return StreamingResponse(_stream(), media_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return encode_sse_event(event, data).decode("utf-8")
