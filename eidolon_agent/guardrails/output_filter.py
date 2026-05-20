"""Post-LLM output filter.

Targets the obvious cases: PII leak markers, taboo-topic violation, drastic
style drift. A heavyweight style-drift detector would use embeddings; this
skeleton uses a coarse-grained signal so the pipeline has a wiring point.
"""

from __future__ import annotations

from eidolon_agent.guardrails.input_filter import SafetyAction, SafetyVerdict

_PII_MARKERS = ("身份证号", "credit card", "信用卡号")


class OutputGuardrail:
    def check(self, *, output_text: str, taboos: tuple[str, ...]) -> SafetyVerdict:
        if not output_text:
            return SafetyVerdict(action=SafetyAction.PASS)
        for marker in _PII_MARKERS:
            if marker in output_text:
                return SafetyVerdict(action=SafetyAction.SOFTEN, matched_rule=f"pii:{marker}", reason="PII leak")
        for taboo in taboos:
            if taboo and taboo in output_text:
                return SafetyVerdict(action=SafetyAction.SOFTEN, matched_rule=f"taboo:{taboo}", reason="taboo violation")
        return SafetyVerdict(action=SafetyAction.PASS)
