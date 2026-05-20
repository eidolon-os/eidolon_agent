"""Deep-merge Template ⊕ Overlay → CompanionProfile.

Locked fields (per template's ``locked_fields``) are always taken from the
template — overlay overrides on those fields are silently ignored (and logged
once at WARN level). The result is cached in NATS KV keyed by ``persona.resolved.<instance_id>``.
"""

from __future__ import annotations

import logging
from typing import Any

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.core.types.persona import (
    CompanionProfile,
    PersonaOverlay,
    PersonaTemplate,
)
from eidolon_agent.persona.overlay_store import PersonaOverlayStore
from eidolon_agent.persona.template_registry import PersonaTemplateRegistry

_log = logging.getLogger(__name__)


class PersonaResolver:
    """Resolves the runtime CompanionProfile for an AgentInstance."""

    KV_KEY_PREFIX = "persona.resolved."

    def __init__(
        self,
        templates: PersonaTemplateRegistry,
        overlays: PersonaOverlayStore,
        *,
        kv_store=None,  # KVStore protocol, optional
    ) -> None:
        self._templates = templates
        self._overlays = overlays
        self._kv = kv_store

    async def resolve(
        self,
        *,
        instance_id: str,
        template_id: str,
        tenant_id: str,
        user_id: str,
        force_refresh: bool = False,
    ) -> CompanionProfile:
        """Compute the merged profile (cached in NATS KV)."""
        if not force_refresh and self._kv is not None:
            cached = await self._kv.get(self.KV_KEY_PREFIX + instance_id)
            if cached:
                return CompanionProfile.model_validate_json(cached)

        tpl = self._templates.get(template_id)
        try:
            overlay = self._overlays.load(tenant_id, user_id, instance_id)
        except NotFoundError:
            overlay = self._overlays.init_empty(
                instance_id=instance_id,
                template_id=template_id,
                template_version=tpl.version,
                tenant_id=tenant_id,
                user_id=user_id,
            )

        profile = _merge(tpl, overlay)
        if self._kv is not None:
            await self._kv.put(
                self.KV_KEY_PREFIX + instance_id,
                profile.model_dump_json().encode(),
            )
        return profile

    async def invalidate(self, instance_id: str) -> None:
        if self._kv is not None:
            await self._kv.delete(self.KV_KEY_PREFIX + instance_id)


def _merge(template: PersonaTemplate, overlay: PersonaOverlay) -> CompanionProfile:
    """Pure function. Overlay overrides win **except** for locked fields."""
    locked = set(template.locked_fields)
    overrides = dict(overlay.overrides or {})

    def _ov(key: str, default: Any) -> Any:
        if key in locked:
            if key in overrides:
                _log.warning(
                    "overlay attempts to override locked field %r on template %s — ignored",
                    key,
                    template.template_id,
                )
            return default
        return overrides.get(key, default)

    # speech_style: shallow merge — overlay's nested keys override template's.
    style = template.speech_style_base.model_copy()
    if "speech_style" in overrides and "speech_style" not in locked:
        s_over = overrides["speech_style"]
        if isinstance(s_over, dict):
            for k, v in s_over.items():
                if hasattr(style, k):
                    style = style.model_copy(update={k: v})

    big5 = template.big5_base
    if "big5" in overrides and "big5" not in locked:
        b_over = overrides["big5"]
        if isinstance(b_over, dict):
            big5 = big5.model_copy(update=b_over)

    values = tuple(_ov("values", template.values_base))
    taboos = tuple(template.taboos)  # taboos are locked-by-policy (only added, never removed)
    skills = template.skills

    voice_id = overlay.voice_id or template.default_voice_id
    avatar_id = overlay.avatar_id or template.default_avatar_id

    # Combine skill list with overlay's unlocked_skills tags.
    if overlay.unlocked_skills:
        unlocked = set(overlay.unlocked_skills)
        skills = tuple(
            s.model_copy(update={"enabled": True}) if s.name in unlocked else s
            for s in skills
        )

    return CompanionProfile(
        instance_id=overlay.instance_id,
        template_id=template.template_id,
        template_version=template.version,
        name=overlay.nickname_alias or template.name,
        archetype=template.archetype,
        pronouns=template.pronouns,
        big5=big5,
        speech_style=style,
        values=values,
        taboos=taboos,
        skills=skills,
        voice_id=voice_id,
        avatar_id=avatar_id,
        milestones=overlay.milestones,
        bond_history_summary=overlay.bond_history_summary,
        nickname_alias=overlay.nickname_alias,
        locked_fields=template.locked_fields,
    )


__all__ = ["PersonaResolver"]
