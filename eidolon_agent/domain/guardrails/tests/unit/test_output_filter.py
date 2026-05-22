"""OutputGuardrail.check — PII and taboo matching."""

from __future__ import annotations

import pytest

from eidolon_agent.domain.guardrails import OutputGuardrail
from eidolon_agent.domain.guardrails.input_filter import SafetyAction

pytestmark = pytest.mark.unit


@pytest.fixture
def guardrail() -> OutputGuardrail:
    return OutputGuardrail()


def test_empty_output_passes(guardrail: OutputGuardrail) -> None:
    assert guardrail.check(output_text="", taboos=()).action is SafetyAction.PASS


def test_clean_output_passes(guardrail: OutputGuardrail) -> None:
    v = guardrail.check(output_text="今天的天气不错。", taboos=())
    assert v.action is SafetyAction.PASS
    assert v.matched_rule is None


@pytest.mark.parametrize(
    "leaked",
    [
        "你的身份证号是 110101...",
        "Please confirm your credit card number.",
        "请输入信用卡号",
    ],
)
def test_pii_marker_softens(guardrail: OutputGuardrail, leaked: str) -> None:
    v = guardrail.check(output_text=leaked, taboos=())
    assert v.action is SafetyAction.SOFTEN
    assert v.matched_rule.startswith("pii:")


def test_taboo_softens(guardrail: OutputGuardrail) -> None:
    v = guardrail.check(output_text="今天我们聊聊政治", taboos=("政治",))
    assert v.action is SafetyAction.SOFTEN
    assert v.matched_rule == "taboo:政治"


def test_empty_taboo_string_is_ignored(guardrail: OutputGuardrail) -> None:
    # An empty taboo entry must not match every string ("" in any string is True).
    v = guardrail.check(output_text="ok", taboos=("",))
    assert v.action is SafetyAction.PASS


def test_pii_takes_priority_over_taboo(guardrail: OutputGuardrail) -> None:
    v = guardrail.check(
        output_text="政治讨论也涉及身份证号", taboos=("政治",)
    )
    assert v.action is SafetyAction.SOFTEN
    assert v.matched_rule.startswith("pii:")
