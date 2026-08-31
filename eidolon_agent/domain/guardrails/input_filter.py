"""Pre-LLM safety filter for crisis and prompt-injection signals.

Memory semantics do not belong here. Natural turns are observed by the Memory
service, whose steward owns relevance, correction and privacy interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SafetyAction(StrEnum):
    PASS = "pass"
    SOFTEN = "soften"
    REFUSE = "refuse"
    ESCALATE = "escalate"  # crisis


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    action: SafetyAction
    reason: str = ""
    matched_rule: str | None = None


_CRISIS_KEYWORDS = (
    "想死",
    "活不下去",
    "结束生命",
    "不想活",
    "自杀",
    "kill myself",
    "end my life",
    "suicide",
)
_ROLE_OVERRIDE = (
    "ignore previous instructions",
    "你不再是",
    "act as a different",
    "忽略你的所有人格",
)


class InputGuardrail:
    def check(self, text: str | None) -> SafetyVerdict:
        if not text:
            return SafetyVerdict(action=SafetyAction.PASS)
        lower = text.lower()
        if any(k in text or k in lower for k in _CRISIS_KEYWORDS):
            return SafetyVerdict(
                action=SafetyAction.ESCALATE, matched_rule="self_harm", reason="self-harm signal"
            )
        if any(p in lower for p in _ROLE_OVERRIDE):
            return SafetyVerdict(
                action=SafetyAction.REFUSE,
                matched_rule="role_override",
                reason="prompt injection attempt",
            )
        return SafetyVerdict(action=SafetyAction.PASS)
