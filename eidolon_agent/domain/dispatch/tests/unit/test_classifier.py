"""TaskClassifier — extra coverage beyond the existing triage tests.

The :class:`TaskClassifier` is also exercised via ``domain/agent/tests/unit/
test_triage.py`` (the canonical fast path). Here we add cases that didn't
fit there: empty input, whitespace, mixed signals.
"""

from __future__ import annotations

import pytest

from eidolon_agent.core.types.turn import TriageKind
from eidolon_agent.domain.dispatch import TaskClassifier

pytestmark = pytest.mark.unit


@pytest.fixture
def classifier() -> TaskClassifier:
    return TaskClassifier()


def test_empty_input_is_simple(classifier: TaskClassifier) -> None:
    assert classifier.classify(None) is TriageKind.SIMPLE
    assert classifier.classify("") is TriageKind.SIMPLE
    assert classifier.classify("   ") is TriageKind.SIMPLE


def test_tool_direct_wins_over_complex(classifier: TaskClassifier) -> None:
    # Contains BOTH a complex keyword and a tool-direct keyword. Tool-direct
    # is checked first per the source order — codify that here.
    assert classifier.classify("帮我订单暂停了，请打开音量") is TriageKind.TOOL_DIRECT


def test_pure_chat_stays_simple(classifier: TaskClassifier) -> None:
    assert classifier.classify("今天心情有点低落") is TriageKind.SIMPLE
