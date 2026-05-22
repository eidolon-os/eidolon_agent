"""Tiny rule-based task classifier.

The real classifier uses a small LLM; this rule layer covers obvious cases
deterministically and acts as the fallback when the LLM router is degraded.
"""

from __future__ import annotations

from eidolon_agent.core.types.turn import TriageKind

_COMPLEX_KEYWORDS_ZH = (
    "帮我订", "帮我查", "帮我安排", "帮我买", "帮我预约", "帮我搜",
    "下单", "出差", "行程", "机票", "酒店",
)
_TOOL_DIRECT_KEYWORDS_ZH = (
    "打开", "关闭", "切歌", "暂停", "调高音量", "调低音量",
)


class TaskClassifier:
    """Pure function. Returns TriageKind. Side-effect-free."""

    def classify(self, text: str | None) -> TriageKind:
        if not text:
            return TriageKind.SIMPLE
        t = text.strip()
        if any(kw in t for kw in _TOOL_DIRECT_KEYWORDS_ZH):
            return TriageKind.TOOL_DIRECT
        if any(kw in t for kw in _COMPLEX_KEYWORDS_ZH):
            return TriageKind.COMPLEX_LONG
        return TriageKind.SIMPLE
