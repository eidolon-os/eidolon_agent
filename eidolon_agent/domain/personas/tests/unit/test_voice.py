"""PersonaVoice: persona card resolution + hot-path phrase override."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from eidolon_agent.domain.personas.voice import PersonaCard, PersonaVoice

pytestmark = pytest.mark.unit


def test_persona_card_to_prompt_includes_identity_and_tone() -> None:
    card = PersonaCard(
        name="洁枝",
        archetype="温柔照护者",
        pronouns="我/你",
        values=("真诚",),
        style_hints=("语气温柔",),
        tone_hint="心情不错",
    )
    prompt = card.to_prompt()
    assert "洁枝" in prompt
    assert "温柔照护者" in prompt
    assert "心情不错" in prompt


async def test_card_returns_none_without_service() -> None:
    voice = PersonaVoice(personas_service=None)
    assert await voice.card(owner_id="o", companion_id="c") is None


async def test_card_degrades_to_none_on_service_error() -> None:
    class _Boom:
        async def get_snapshot(self, **_):
            raise RuntimeError("persona backend down")

    voice = PersonaVoice(_Boom())
    assert await voice.card(owner_id="o", companion_id="c") is None


def test_phrase_falls_back_to_default_without_override() -> None:
    persona = SimpleNamespace(style_compiler=SimpleNamespace(spoken_phrases={}))
    assert (
        PersonaVoice.phrase(persona, "slow_tool_hint", "稍等一下")
        == "稍等一下"
    )
    # None persona (e.g. unresolved) also degrades to default.
    assert PersonaVoice.phrase(None, "slow_tool_hint", "稍等一下") == "稍等一下"


def test_phrase_uses_persona_override_when_present() -> None:
    persona = SimpleNamespace(
        style_compiler=SimpleNamespace(
            spoken_phrases={"slow_tool_hint": "喵，我在查啦～"}
        )
    )
    assert (
        PersonaVoice.phrase(persona, "slow_tool_hint", "稍等一下")
        == "喵，我在查啦～"
    )


class _StubPersonas:
    def __init__(self, instance) -> None:
        self._instance = instance

    async def get_snapshot(self, *, owner_id, companion_id):
        return SimpleNamespace(instance=self._instance, prompt_hint="心情不错")


def _persona(spoken_phrases=None):
    return SimpleNamespace(
        style_compiler=SimpleNamespace(spoken_phrases=spoken_phrases or {})
    )


async def test_proactive_decision_uses_primary_text_when_present() -> None:
    voice = PersonaVoice(_StubPersonas(_persona()))
    d = await voice.proactive_decision(
        owner_id="o",
        companion_id="c",
        intent="long_task_done",
        primary_text="资料整理好啦",
        fallback_default="我弄好了",
    )
    assert d.text == "资料整理好啦"
    assert d.intent == "long_task_done"


async def test_proactive_decision_fallback_is_framed_not_raw() -> None:
    # No primary text (summary failed) → clean fallback, never raw output.
    voice = PersonaVoice(_StubPersonas(_persona()))
    d = await voice.proactive_decision(
        owner_id="o",
        companion_id="c",
        intent="long_task_done",
        primary_text="",
        fallback_default="我把那件事弄好了。",
    )
    assert d.text == "我把那件事弄好了。"


async def test_proactive_decision_honours_persona_phrase_override() -> None:
    persona = _persona(
        {"proactive_long_task_done": "搞定咯，人家很快的～"}
    )
    voice = PersonaVoice(_StubPersonas(persona))
    d = await voice.proactive_decision(
        owner_id="o",
        companion_id="c",
        intent="long_task_done",
        primary_text="",
        fallback_default="我弄好了",
    )
    assert d.text == "搞定咯，人家很快的～"


async def test_proactive_decision_degrades_without_personas() -> None:
    voice = PersonaVoice(personas_service=None)
    d = await voice.proactive_decision(
        owner_id="o",
        companion_id="c",
        intent="long_task_done",
        primary_text="",
        fallback_default="我弄好了",
    )
    assert d.text == "我弄好了"
    assert d.style_hint == "long_task_done"
