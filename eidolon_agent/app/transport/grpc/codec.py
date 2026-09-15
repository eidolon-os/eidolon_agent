"""Bidirectional codec between proto messages and core types."""

from __future__ import annotations

from eidolon_sdk.core.protobuf import protobuf_struct_to_dict
from google.protobuf import struct_pb2

from eidolon_agent.app.transport.grpc.proto import pb
from eidolon_agent.core.types.turn import TurnEvent, TurnEventKind

_KIND_TO_PROTO = {
    TurnEventKind.STATE: pb.TurnEvent.STATE,
    TurnEventKind.DELTA: pb.TurnEvent.DELTA,
    TurnEventKind.TOOL_CALL: pb.TurnEvent.TOOL_CALL,
    TurnEventKind.TOOL_RESULT: pb.TurnEvent.TOOL_RESULT,
    TurnEventKind.CITATION: pb.TurnEvent.CITATION,
    TurnEventKind.USAGE: pb.TurnEvent.USAGE,
    TurnEventKind.DONE: pb.TurnEvent.DONE,
    TurnEventKind.ERROR: pb.TurnEvent.ERROR,
    TurnEventKind.ACK: pb.TurnEvent.ACK,
    TurnEventKind.PROGRESS: pb.TurnEvent.PROGRESS,
    TurnEventKind.PRESENTATION: pb.TurnEvent.PRESENTATION,
    TurnEventKind.HANDOFF: pb.TurnEvent.HANDOFF,
}


def turn_event_to_proto(ev: TurnEvent) -> pb.TurnEvent:
    out = pb.TurnEvent(
        turn_id=ev.turn_id,
        seq=ev.seq,
        kind=_KIND_TO_PROTO.get(ev.kind, pb.TurnEvent.KIND_UNSPECIFIED),
        ts=ev.ts,
    )
    if ev.kind is TurnEventKind.PRESENTATION:
        from eidolon_sdk.biz.presentation import ResponseIntent
        intent = ResponseIntent.model_validate(ev.data)
        out.presentation.CopyFrom(pb.ResponseIntent(**intent.model_dump(exclude_none=True)))
    else:
        out.data.update(ev.data)
    return out


def struct_to_dict(struct: struct_pb2.Struct | None) -> dict:
    return protobuf_struct_to_dict(struct)
