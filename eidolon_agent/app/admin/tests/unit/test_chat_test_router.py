"""Admin chat-test router helpers."""

from __future__ import annotations

import json

from google.protobuf.struct_pb2 import Struct

from eidolon_agent.app.admin.routers.chat_test import _sse
from eidolon_agent.app.transport.grpc.codec import struct_to_dict


def test_sse_serializes_struct_tool_payload() -> None:
    payload = Struct()
    payload.update({
        "name": "delegate_to_coworker",
        "content": {"accepted": True, "task_id": "task-1"},
    })

    raw = _sse("event", {"kind": "TOOL_RESULT", "data": struct_to_dict(payload)})

    assert raw.startswith("event: event\n")
    data_line = next(line for line in raw.splitlines() if line.startswith("data: "))
    decoded = json.loads(data_line.removeprefix("data: "))
    assert decoded["data"]["content"]["task_id"] == "task-1"
