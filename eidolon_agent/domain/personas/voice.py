"""Persona-aware rendering for asynchronous and canned outward text."""

from __future__ import annotations

from dataclasses import dataclass

from eidolon_sdk.biz.persona import PersonaGenome

from eidolon_agent.domain.personas.types import PersonaProactiveDecision


@dataclass(frozen=True, slots=True)
class PersonaCard:
    name: str
    archetype: str
    self_concept: str = ""
    values: tuple[str, ...] = ()
    style_hints: tuple[str, ...] = ()
    tone_hint: str = ""

    def to_prompt(self) -> str:
        lines = [f"你是「{self.name}」（{self.archetype}）。以第一人称和自己的口吻说话。"]
        if self.self_concept:
            lines.append("自我认知：" + self.self_concept)
        if self.values:
            lines.append("价值观：" + "；".join(self.values))
        if self.style_hints:
            lines.append("表达方式：" + "；".join(self.style_hints))
        if self.tone_hint:
            lines.append("当前状态：" + self.tone_hint)
        return "\n".join(lines)


def card_from_genome(genome: PersonaGenome, *, tone_hint: str = "") -> PersonaCard:
    return PersonaCard(
        name=genome.constitution.name,
        archetype=genome.constitution.archetype,
        self_concept=genome.constitution.self_concept,
        values=tuple(genome.constitution.values[:3]),
        style_hints=tuple(genome.expression.behavior_guidance[:3]),
        tone_hint=tone_hint,
    )


class PersonaVoice:
    def __init__(self, personas_service) -> None:
        self._personas = personas_service

    async def card(self, *, owner_id: str, companion_id: str) -> PersonaCard | None:
        if self._personas is None or not owner_id or not companion_id:
            return None
        try:
            snapshot = await self._personas.get_snapshot(
                owner_id=owner_id,
                companion_id=companion_id,
            )
        except Exception:
            return None
        return card_from_genome(
            snapshot.stored.genome,
            tone_hint=snapshot.prompt_hint,
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
        text = primary_text.strip()
        resolved_style = style_hint or intent
        genome: PersonaGenome | None = None
        if self._personas is not None and owner_id and companion_id:
            try:
                snapshot = await self._personas.get_snapshot(
                    owner_id=owner_id,
                    companion_id=companion_id,
                )
                genome = snapshot.stored.genome
            except Exception:
                genome = None
        if not text:
            text = self.phrase(genome, f"proactive_{intent}", fallback_default).strip()
        resolved_style = self.phrase(
            genome,
            f"proactive_style_{intent}",
            resolved_style,
        )
        return PersonaProactiveDecision(
            companion_id=companion_id,
            owner_id=owner_id,
            intent=intent,
            text=text,
            style_hint=resolved_style,
        )

    @staticmethod
    def phrase(genome: PersonaGenome | None, key: str, default: str) -> str:
        if genome is None:
            return default
        value = genome.expression.signature_phrases.get(key)
        return value if isinstance(value, str) and value.strip() else default


__all__ = ["PersonaCard", "PersonaVoice", "card_from_genome"]
