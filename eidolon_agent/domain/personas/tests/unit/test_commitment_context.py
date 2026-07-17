from __future__ import annotations

import pytest

from eidolon_agent.core.types.memory import ActiveCommitment
from eidolon_agent.domain.personas.realizer import PersonaRealizer

pytestmark = pytest.mark.unit


def test_realizer_formats_only_active_commitment_context() -> None:
    text = PersonaRealizer().realize_commitment_context(
        [
            ActiveCommitment(
                commitment_id="commitment-1",
                promisor="小忆",
                predicate="promised",
                action="周六陪 owner 去恐龙园",
                status="confirmed",
                beneficiaries=("owner",),
                participants=("朋友甲", "朋友乙"),
                condition="天气合适",
                due_at="2026-07-18T09:00:00+08:00",
                revision=3,
            )
        ]
    )

    assert "id=commitment-1" in text
    assert "status=confirmed" in text
    assert "内容=周六陪 owner 去恐龙园" in text
    assert "参与者=朋友甲、朋友乙" in text
    assert "条件=天气合适" in text
    assert "到期时间=2026-07-18T09:00:00+08:00" in text
