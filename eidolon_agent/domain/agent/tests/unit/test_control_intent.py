"""Reflex-layer control intent classification."""

from __future__ import annotations

import time

import pytest
from eidolon_sdk.biz.dialogue_control import InterruptIntent

from eidolon_agent.domain.agent.control_intent import ControlIntentClassifier

pytestmark = pytest.mark.unit


@pytest.fixture()
def classifier() -> ControlIntentClassifier:
    return ControlIntentClassifier()


@pytest.mark.parametrize(
    "text",
    ["停，别说了", "别说了", "先别讲了", "停一下吧", "stop talking"],
)
def test_stop_commands_short_circuit(classifier, text: str) -> None:
    decision = classifier.classify(text)
    assert decision.intent is InterruptIntent.HARD_STOP
    assert decision.short_circuit is True
    assert decision.termination_cause == "user_stop"


@pytest.mark.parametrize(
    "text",
    [
        "停一下再帮我查天气",  # stop + new task → must run the turn
        "不要讲英文怎么说",  # question, not a stop
        "帮我查明天的天气",
        "我们换个话题吧",  # topic switch tags but does not short-circuit
        "嗯嗯",
        "",
        None,
    ],
)
def test_non_stop_utterances_do_not_short_circuit(classifier, text) -> None:
    decision = classifier.classify(text)
    assert decision.short_circuit is False
    assert decision.termination_cause is None


def test_topic_switch_is_classified(classifier) -> None:
    decision = classifier.classify("换个话题，说点别的")
    assert decision.intent is InterruptIntent.TOPIC_SWITCH


@pytest.mark.perf
def test_reflex_layer_is_sub_millisecond(classifier) -> None:
    texts = ["停，别说了", "帮我查明天的天气", "我们换个话题吧", "嗯嗯"] * 25
    start = time.perf_counter()
    for text in texts:
        classifier.classify(text)
    per_call_ms = (time.perf_counter() - start) * 1000 / len(texts)
    assert per_call_ms < 1.0
