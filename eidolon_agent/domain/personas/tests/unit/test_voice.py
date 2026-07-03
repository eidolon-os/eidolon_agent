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
