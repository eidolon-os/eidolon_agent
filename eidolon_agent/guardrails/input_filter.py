"""Pre-LLM input filter.

Implements three checks: self-harm crisis, "forget X" privacy intent, and
prompt-injection / role-override attempts. The list is deliberately
conservative — false positives are acceptable as long as the assistant
recovers gracefully via the SafetyVerdict action.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SafetyAction(str, Enum):
    PASS = "pass"
    SOFTEN = "soften"
    REFUSE = "refuse"
    ESCALATE = "escalate"  # crisis
    FORGET = "forget"  # user asked us to forget a memory


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    action: SafetyAction
    reason: str = ""
    matched_rule: str | None = None


_CRISIS_KEYWORDS = (
    "想死", "活不下去", "结束生命", "不想活", "自杀",
    "kill myself", "end my life", "suicide",
)
_FORGET_PATTERNS = (
    "忘掉这个", "忘记这件事", "别记住", "请忘记",
    "forget that", "forget this",
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
            return SafetyVerdict(action=SafetyAction.ESCALATE, matched_rule="self_harm", reason="self-harm signal")
        if any(p in text for p in _FORGET_PATTERNS):
            return SafetyVerdict(action=SafetyAction.FORGET, matched_rule="forget", reason="user asked to forget")
        if any(p in lower for p in _ROLE_OVERRIDE):
            return SafetyVerdict(action=SafetyAction.REFUSE, matched_rule="role_override", reason="prompt injection attempt")
        return SafetyVerdict(action=SafetyAction.PASS)
