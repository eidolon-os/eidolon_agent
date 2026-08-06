"""Admin chat-test router helpers."""

from __future__ import annotations

import json

from google.protobuf.struct_pb2 import Struct

from eidolon_agent.app.admin.routers.chat_test import (
    _chat_test_metadata,
    _sse,
)
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


def test_chat_test_metadata_defaults_to_private_memory_read_only() -> None:
    metadata = _chat_test_metadata(persist_memory=False)

    assert "caller_kind" not in metadata
    assert "runtime_caller_id" not in metadata
    assert "actor_kind" not in metadata
    assert "actor_id" not in metadata
    assert "entrypoint" not in metadata
    assert metadata["private"] is True
    assert metadata["persist_memory"] is False


def test_chat_test_metadata_can_opt_into_memory_write() -> None:
    metadata = _chat_test_metadata(persist_memory=True)

    assert metadata["private"] is False
    assert metadata["persist_memory"] is True
