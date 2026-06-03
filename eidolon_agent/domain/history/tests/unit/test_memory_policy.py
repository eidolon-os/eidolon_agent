"""Memory write candidate classification."""

from __future__ import annotations

import pytest

from eidolon_agent.domain.memory_policy import (
    MemoryWriteDispositionKind,
    classify_memory_write,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("哈哈你好", MemoryWriteDispositionKind.IGNORE),
        ("今天我去了医院", MemoryWriteDispositionKind.EPISODIC_ONLY),
        ("以后叫我小满", MemoryWriteDispositionKind.SEMANTIC_UPSERT),
        ("明天提醒我喝水", MemoryWriteDispositionKind.PROMISE_CREATE),
        ("我的身份证是123", MemoryWriteDispositionKind.SENSITIVE_REQUIRES_CONSENT),
    ],
)
def test_classify_memory_write(text: str, kind: MemoryWriteDispositionKind) -> None:
    assert classify_memory_write(user_text=text).kind is kind


def test_disposition_metadata_is_audit_friendly() -> None:
    out = classify_memory_write(user_text="以后叫我小满")

    assert out.to_metadata() == {
        "memory_write_disposition": "semantic_upsert",
        "memory_write_reason": "stable_preference_or_identity",
        "memory_policy_version": "agent_memory_policy.v1",
    }
