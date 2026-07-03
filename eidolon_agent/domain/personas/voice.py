"""PersonaVoice — the single place outward text is put in a companion's voice.

Persona differentiation must not stop at the conversation turn: when the
companion reports a finished long task, wakes the user proactively, or utters
a canned line, it should still sound like *this* companion, not a generic
assistant. PersonaVoice is that unified rendering layer.

Two modes, split by where the text is produced:

* ``card(owner, companion)`` — a compact persona/tone descriptor injected into
  the prompts of **async** LLM-backed outputs (long-task summary, proactive
  report). These run off the hot path, so an extra persona-conditioned LLM
  call is fine.
* ``phrase(persona, key, default)`` — a genome-defined canned line for
  **hot-path** utterances (tool preamble, slow-tool hint, filler, refuse).
  These must be instant, so they are template lookups, never LLM calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from eidolon_agent.domain.personas.types import (
    CompanionPersona,
    PersonaProactiveDecision,
)


@dataclass(frozen=True, slots=True)
class PersonaCard:
    """Compact, prompt-injectable description of who is speaking."""

    name: str
    archetype: str
    pronouns: str
    values: tuple[str, ...] = ()
    style_hints: tuple[str, ...] = ()
    tone_hint: str = ""

    def to_prompt(self) -> str:
        lines = [f"你是「{self.name}」（{self.archetype}）。以第一人称、你的口吻说话。"]
        if self.pronouns:
            lines.append(f"称谓/代词：{self.pronouns}")
        if self.values:
            lines.append("价值观：" + "；".join(self.values))
        if self.style_hints:
            lines.append("表达风格：" + "；".join(self.style_hints))
        if self.tone_hint:
            lines.append("当前语气：" + self.tone_hint)
        return "\n".join(lines)


def card_from_persona(
    persona: CompanionPersona, *, tone_hint: str = ""
) -> PersonaCard:
    style = persona.style_compiler
    return PersonaCard(
        name=persona.metadata.name,
        archetype=persona.metadata.archetype,
        pronouns=persona.identity_core.base_pronouns,
        values=tuple(persona.identity_core.values[:3]),
        style_hints=tuple(style.base_instructions[:3]),
        tone_hint=tone_hint,
    )


class PersonaVoice:
    """Provides persona cards (async outputs) and canned phrasing (hot path)."""

    def __init__(self, personas_service) -> None:
        self._personas = personas_service

    async def card(self, *, owner_id: str, companion_id: str) -> PersonaCard | None:
        """Compact persona descriptor for injecting into async output prompts.

        Best-effort: returns None if the persona can't be resolved so callers
        degrade to a generic voice rather than failing the output entirely.
        """
        if self._personas is None or not owner_id or not companion_id:
            return None
        try:
            snapshot = await self._personas.get_snapshot(
                owner_id=owner_id, companion_id=companion_id
            )
        except Exception:
            return None
        return card_from_persona(
            snapshot.instance, tone_hint=snapshot.prompt_hint or ""
        )

    async def proactive_decision(
        self,
        *,
        owner_id: str,
        companion_id: str,
        intent: str,
        primary_text: str = "",
        fallback_default: str = "",
        style_hint: str = "",
    ) -> PersonaProactiveDecision:
        """Assemble a persona-consistent proactive utterance (off hot path).

        When the companion speaks unprompted (e.g. a finished long task), the
        line must still sound like this companion and must NEVER be a raw data
        dump. ``primary_text`` is the already-persona-rendered content (e.g. the
        LLM summary); when it is empty we fall back to a persona-overridable
        canned line (``spoken_phrases["proactive_<intent>"]``), never to raw
        output. Resolves the persona once; degrades gracefully to the defaults
        if the persona can't be loaded.
        """
        text = (primary_text or "").strip()
        resolved_style = style_hint or intent
        instance: CompanionPersona | None = None
        if self._personas is not None and owner_id and companion_id:
            try:
                snapshot = await self._personas.get_snapshot(
                    owner_id=owner_id, companion_id=companion_id
                )
                instance = snapshot.instance
            except Exception:
                instance = None
        if not text:
            text = self.phrase(
                instance, f"proactive_{intent}", fallback_default
            ).strip()
        resolved_style = self.phrase(
            instance, f"proactive_style_{intent}", resolved_style
        )
        return PersonaProactiveDecision(
            companion_id=companion_id,
            owner_id=owner_id,
            intent=intent,
            text=text,
            style_hint=resolved_style,
        )

    @staticmethod
    def phrase(persona: CompanionPersona | None, key: str, default: str) -> str:
        """Hot-path canned line, persona-overridable, template only (no LLM).

        Personas may override outward canned lines (tool preamble, slow-tool
        hint, filler, refuse) via ``style_compiler.spoken_phrases[key]``.
        Falls back to ``default`` so callers always get a usable line.
        """
        if persona is None:
            return default
        phrases = getattr(persona.style_compiler, "spoken_phrases", None)
        if not phrases:
            return default
        value = phrases.get(key)
        return value if isinstance(value, str) and value.strip() else default
