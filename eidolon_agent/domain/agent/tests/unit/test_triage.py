"""TriageKind classifier coverage."""


import pytest

from eidolon_agent.core.types.turn import TriageKind
from eidolon_agent.domain.dispatch.classifier import TaskClassifier

pytestmark = pytest.mark.unit

def test_simple_default():
    c = TaskClassifier()
    assert c.classify("你好") is TriageKind.SIMPLE
    assert c.classify("") is TriageKind.SIMPLE
    assert c.classify(None) is TriageKind.SIMPLE


def test_complex_keywords():
    c = TaskClassifier()
    assert c.classify("帮我订下周三的机票") is TriageKind.COMPLEX_LONG
    assert c.classify("帮我安排出差行程") is TriageKind.COMPLEX_LONG


def test_tool_direct_keywords():
    c = TaskClassifier()
    assert c.classify("打开音乐") is TriageKind.TOOL_DIRECT
    assert c.classify("切歌") is TriageKind.TOOL_DIRECT
