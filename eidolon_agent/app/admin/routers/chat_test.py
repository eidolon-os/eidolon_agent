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

from eidolon_agent.app.transport.grpc.codec import struct_to_dict
from eidolon_agent.app.transport.grpc.proto import pb, pbg
from eidolon_agent.core.types.identity import derive_runtime_caller_id

_log = logging.getLogger(__name__)

router = APIRouter()


class ChatTestRequest(BaseModel):
    owner_id: str = "demo"
    companion_id: str = "companion-demo"
    text: str = ""
    persist_memory: bool = False


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
    genome = await data_store.persona_repo.get_genome(companion.current_genome_id)
    if genome is None or genome.status != "committed":
        raise RuntimeError("companion current genome is not committed")

    await _refresh_memory_discovery_for_admin_chat(
        request,
        owner_id=body.owner_id,
        companion_id=body.companion_id,
    )

    jwt_secret = resolve_shared_secret(settings.runtime_token.jwt_secret)
    if not jwt_secret:
        raise RuntimeError("runtime token secret not configured")
    actor_id = f"admin-chat-test:{body.owner_id}:{body.companion_id}"
    runtime_caller_id = _admin_runtime_caller_id(
        owner_id=body.owner_id,
        companion_id=body.companion_id,
        actor_id=actor_id,
    )
    runtime_token, _ = sign_runtime_token(
        secret=jwt_secret,
        algorithm=settings.runtime_token.jwt_algorithm,
        actor_kind="admin_console",
        actor_id=actor_id,
        owner_id=body.owner_id,
        companion_id=body.companion_id,
        memory_realm_id=companion.default_memory_realm_id,
        genome_id=companion.current_genome_id,
        schema_version=genome.schema_version,
        genome_hash=genome.genome_hash,
        realizer_version=genome.realizer_version,
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
                        runtime_caller_id=runtime_caller_id,
                        actor_id=actor_id,
                    )
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
                metadata=(("authorization", f"Bearer {runtime_token}"),),
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


def _chat_test_metadata(
    *,
    persist_memory: bool,
    runtime_caller_id: str = "",
    actor_id: str = "",
) -> dict:
    return {
        "caller_kind": "admin_test",
        "runtime_caller_id": runtime_caller_id,
        "actor_kind": "admin_console",
        "actor_id": actor_id,
        "caller_display_name": "Admin Chat Test",
        "entrypoint": "admin_chat_test",
        "private": not persist_memory,
        "persist_memory": persist_memory,
    }


def _admin_runtime_caller_id(*, owner_id: str, companion_id: str, actor_id: str) -> str:
    return derive_runtime_caller_id(
        owner_id=owner_id,
        companion_id=companion_id,
        actor_kind="admin_console",
        actor_id=actor_id,
    )


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
