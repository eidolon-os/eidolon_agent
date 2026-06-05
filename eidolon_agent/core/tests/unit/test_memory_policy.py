"""Memory write candidate classification."""

from __future__ import annotations

import pytest

from eidolon_agent.core.types import (
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
        ("以后请叫我小满", MemoryWriteDispositionKind.SEMANTIC_UPSERT),
        ("我现在住在杭州", MemoryWriteDispositionKind.SEMANTIC_UPSERT),
        ("更正一下，我现在住在杭州，不是上海", MemoryWriteDispositionKind.SEMANTIC_UPSERT),
        ("我现在住在哪里？", MemoryWriteDispositionKind.IGNORE),
        ("你应该怎么称呼我？", MemoryWriteDispositionKind.IGNORE),
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


def test_classify_memory_write_ignores_assistant_chitchat_markers() -> None:
    out = classify_memory_write(
        user_text="我住在上海。",
        assistant_text="上海呀，你喜欢那里的生活节奏吗？今天有什么安排？",
    )

    assert out.kind is MemoryWriteDispositionKind.SEMANTIC_UPSERT
