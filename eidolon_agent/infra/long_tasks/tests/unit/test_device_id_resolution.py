"""device_id resolution for the proactive wake event (Phase 3).

The proactive event must carry the owning device_id so hub can route the wake
(send_command room.join). Source of truth is the denormalized
``LongTaskRecord.device_id`` (← caller identity / turns.device_id); until that is
wired end-to-end we fall back to parsing the livekit ``conversation_id``.
"""

from __future__ import annotations

import pytest

from eidolon_agent.infra.long_tasks.mementos import _device_id_from_conversation_id


@pytest.mark.parametrize(
    "conversation_id, expected",
    [
        # livekit triple: prefix:<MAC with colons>:<room without colons>
        ("livekit:1c:db:d4:7a:ef:0c:device-1c-db-d4-7a-ef-0c", "1c:db:d4:7a:ef:0c"),
        # a non-MAC identity still recovers as the middle segment(s)
        ("livekit:sim-device:device-sim", "sim-device"),
        # not a triple / unparseable → None (wake skipped upstream)
        ("c1", None),
        ("livekit:room", None),
        ("", None),
        (None, None),
    ],
)
def test_device_id_from_conversation_id(conversation_id, expected):
    assert _device_id_from_conversation_id(conversation_id) == expected
