"""Admin: gRPC chat test over the real Chat bidi runtime path."""

from __future__ import annotations

import logging
import uuid

import grpc
from eidolon_sdk.biz.runtime import resolve_shared_secret, sign_runtime_token
from eidolon_sdk.core.streaming import encode_sse_event
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from google.protobuf import struct_pb2
from pydantic import BaseModel

from eidolon_agent.app.admin.authority import AUTHORITY_DEPENDENCIES
from eidolon_agent.app.transport.grpc.codec import struct_to_dict
from eidolon_agent.app.transport.grpc.proto import pb, pbg

_log = logging.getLogger(__name__)

router = APIRouter(dependencies=AUTHORITY_DEPENDENCIES)


class ChatTestRequest(BaseModel):
    owner_id: str = "demo"
    companion_id: str = "companion-demo"
    text: str = ""
    persist_memory: bool = False


@router.post("/chat/test")
async def chat_test(body: ChatTestRequest, request: Request):
    """Stream a turn over the real authenticated gRPC Chat bidi path."""
    settings = request.app.state.settings
    runtime_authority = getattr(request.app.state, "runtime_authority", None)
    if runtime_authority is None:
        raise RuntimeError("runtime_authority not configured")
    await runtime_authority.resolve(
        owner_id=body.owner_id,
        companion_id=body.companion_id,
    )

    await _refresh_memory_discovery_for_admin_chat(
        request,
        owner_id=body.owner_id,
        companion_id=body.companion_id,
    )

    jwt_secret = resolve_shared_secret(settings.runtime_token.jwt_secret)
    if not jwt_secret:
        raise RuntimeError("runtime token secret not configured")
    session_id = f"admin-chat-{uuid.uuid4().hex}"
    conversation_id = f"admin-test-{uuid.uuid4().hex[:8]}"
    runtime_token, _ = sign_runtime_token(
        secret=jwt_secret,
        algorithm=settings.runtime_token.jwt_algorithm,
        owner_id=body.owner_id,
        companion_id=body.companion_id,
        session_id=session_id,
        scopes=["admin-chat-test"],
        ttl_seconds=600,
    )
    target = f"{settings.grpc.tcp_host}:{settings.grpc.tcp_port}"

    async def _stream():
        channel = grpc.aio.insecure_channel(target)
        try:
            stub = pbg.EidolonAgentStub(channel)
            yield _sse("status", {"message": "token_issued", "owner_id": body.owner_id})

            async def _requests():
                metadata = struct_pb2.Struct()
                metadata.update(
                    _chat_test_metadata(
                        persist_memory=body.persist_memory,
                    )
                )
                yield pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id=uuid.uuid4().hex,
                        conversation_id=conversation_id,
                        text=body.text,
                        input_modality="text",
                        metadata=metadata,
                    )
                )

            stream = stub.Chat(
                _requests(),
                metadata=(("authorization", f"Bearer {runtime_token}"),),
            )
            async for ev in stream:
                kind = pb.TurnEvent.Kind.Name(ev.kind)
                data = struct_to_dict(ev.data) if ev.data else {}
                yield _sse(
                    "event",
                    {
                        "turn_id": ev.turn_id,
                        "seq": ev.seq,
                        "kind": kind,
                        "data": data,
                    },
                )
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


def _chat_test_metadata(
    *,
    persist_memory: bool,
) -> dict:
    return {
        "private": not persist_memory,
        "persist_memory": persist_memory,
    }


async def _refresh_memory_discovery_for_admin_chat(
    request: Request,
    *,
    owner_id: str,
    companion_id: str,
) -> bool:
    memory_refresher = getattr(request.app.state, "memory_discovery_refresher", None)
    if memory_refresher is None:
        return False
    refreshed = await memory_refresher.refresh_once()
    if not refreshed:
        _log.warning(
            "admin chat test memory discovery refresh failed before turn",
            extra={
                "owner_id": owner_id,
                "companion_id": companion_id,
            },
        )
    return refreshed
