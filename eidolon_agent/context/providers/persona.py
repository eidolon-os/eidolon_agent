"""Inject the resolved CompanionProfile as a CRITICAL-weight system segment.

Persona is the *identity* of the agent — it must never be pruned. The
segment is short on purpose: a one-paragraph identity card plus a compact
"do/don't" line. The full overlay/template is exposed via tools when needed.
"""

from __future__ import annotations

from eidolon_agent.core.ports.context import ProviderContext
from eidolon_agent.core.types.context import ContextSegment, SegmentType, SegmentWeight


class PersonaContextProvider:
    name = "persona"
    segment_type = SegmentType.PERSONA
    default_weight = SegmentWeight.CRITICAL
    soft_timeout_s = 0.05  # we already have the resolved profile cached

    def __init__(self, resolver=None, *, instance_locator=None) -> None:
        """``resolver`` = :class:`PersonaResolver`.

        ``instance_locator`` maps ``(tenant_id, user_id, conv_id)`` → ``(instance_id, template_id)``.
        Bootstrap wires both in. Tests pass a callable returning a tuple.
        """
        self._resolver = resolver
        self._locator = instance_locator

    async def provide(self, ctx: ProviderContext) -> list[ContextSegment]:
        if self._resolver is None or self._locator is None:
            return []
        ti = ctx.turn_input
        instance_id, template_id = self._locator(ti.caller.tenant_id, ti.caller.user_id, ti.conversation_id)
        profile = await self._resolver.resolve(
            instance_id=instance_id,
            template_id=template_id,
            tenant_id=ti.caller.tenant_id,
            user_id=ti.caller.user_id,
        )
        content = _format_profile_card(profile)
        return [
            ContextSegment(
                type=self.segment_type,
                weight=self.default_weight,
                content=content,
                tokens=max(1, len(content) // 3),
                source=self.name,
                tags=("persona", profile.template_id),
            )
        ]


def _format_profile_card(profile) -> str:  # type: ignore[no-untyped-def]
    lines = [
        f"你是「{profile.name}」（{profile.archetype}）。",
        f"说话风格：{profile.speech_style.formality} / 句长 {profile.speech_style.sentence_length}"
        f" / emoji {profile.speech_style.emoji_density}",
    ]
    if profile.speech_style.catchphrases:
        lines.append("口头禅：" + "、".join(profile.speech_style.catchphrases))
    if profile.values:
        lines.append("价值观：" + "；".join(profile.values))
    if profile.taboos:
        lines.append("绝不：" + "；".join(profile.taboos))
    if profile.bond_history_summary:
        lines.append("和这位用户的关系摘要：" + profile.bond_history_summary)
    return "\n".join(lines)
