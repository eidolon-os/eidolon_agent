"""Admin: gRPC chat test over the real Chat bidi runtime path."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone

import grpc
from eidolon_sdk.biz.runtime import resolve_shared_secret, sign_device_token
from eidolon_sdk.core.streaming import encode_sse_event
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from google.protobuf import struct_pb2
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
    """Stream a turn over the real authenticated gRPC Chat bidi path."""
    settings = request.app.state.settings
    data_store = getattr(request.app.state, "data_store", None)
    if data_store is None:
        raise RuntimeError("data_store not configured")
    companion = await data_store.companions.get(body.companion_id)
    if companion is None or companion.owner_id != body.owner_id:
        raise RuntimeError("companion not found for owner")
    if not companion.default_memory_realm_id or not companion.current_genome_id:
        raise RuntimeError("companion has no default memory realm or current genome")

    await _refresh_memory_discovery_for_admin_chat(
        request,
        owner_id=body.owner_id,
        companion_id=body.companion_id,
    )

    test_device_id = await _ensure_admin_console_device(
        data_store,
        owner_id=body.owner_id,
        companion_id=body.companion_id,
    )
    jwt_secret = resolve_shared_secret(settings.runtime_token.jwt_secret)
    if not jwt_secret:
        raise RuntimeError("runtime token secret not configured")
    device_token, _ = sign_device_token(
        secret=jwt_secret,
        algorithm=settings.runtime_token.jwt_algorithm,
        device_id=test_device_id,
        owner_id=body.owner_id,
        companion_id=body.companion_id,
        memory_realm_id=companion.default_memory_realm_id,
        genome_id=companion.current_genome_id,
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
                    {
                        "caller_kind": "admin_test",
                        "entrypoint": "admin_chat_test",
                    }
                )
                yield pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id=uuid.uuid4().hex,
                        conversation_id=f"admin-test-{uuid.uuid4().hex[:8]}",
                        text=body.text,
                        metadata=metadata,
                    )
                )

            stream = stub.Chat(
                _requests(),
                metadata=(("authorization", f"Bearer {device_token}"),),
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


def _admin_console_device_id(*, owner_id: str, companion_id: str) -> str:
    digest = hashlib.sha256(f"{owner_id}\0{companion_id}".encode("utf-8")).hexdigest()
    return f"admin-console-{digest[:16]}"


async def _ensure_admin_console_device(
    data_store,
    *,
    owner_id: str,
    companion_id: str,
) -> str:
    device_id = _admin_console_device_id(owner_id=owner_id, companion_id=companion_id)
    now = datetime.now(timezone.utc)
    await data_store.devices.put_device(
        device_id=device_id,
        owner_id=owner_id,
        name=f"Admin Console ({companion_id})",
        kind="admin_console",
        status="active",
        approved_at=now,
        approved_by="admin",
        bound_companion_id=companion_id,
        interaction_mode="admin_test",
        auth_type="runtime_token",
        capabilities_json={"chat_test": True},
        network_json={"source": "eidolon_admin"},
        access_policy_json={"scope": "admin_chat_test"},
        metadata_json={
            "source": "eidolon_agent.admin.chat_test",
            "owner_id": owner_id,
            "companion_id": companion_id,
        },
        last_seen_at=now,
    )
    return device_id


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
