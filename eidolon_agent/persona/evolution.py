"""EvolutionPlanner — propose / guard / apply / rollback PersonaOverlay changes.

Evolution NEVER modifies a PersonaTemplate. All sanctioned drift accumulates
in the overlay. The guard enforces three invariants:

1. Locked fields (per template) cannot be overridden.
2. Big5 deltas cannot exceed the template's ``big5_step_max`` per apply.
3. Taboos can only be added, never removed.

Apply is two-stage: (a) write the new overlay YAML and git-commit; (b) record
an ``EvolutionDelta`` audit row in SQLite. Rollback finds an earlier overlay
version in git history and restores it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

from eidolon_agent.core.errors import EvolutionGuardError
from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.persona import (
    EvolutionDelta,
    PersonaOverlay,
    PersonaTemplate,
)
from eidolon_agent.events.topics import Topics
from eidolon_agent.persona.overlay_store import PersonaOverlayStore
from eidolon_agent.persona.resolver import PersonaResolver
from eidolon_agent.persona.template_registry import PersonaTemplateRegistry

_log = logging.getLogger(__name__)


class EvolutionPlanner:
    def __init__(
        self,
        templates: PersonaTemplateRegistry,
        overlays: PersonaOverlayStore,
        resolver: PersonaResolver,
        *,
        event_bus=None,
        evolution_repo=None,  # EvolutionHistoryRepository, optional
    ) -> None:
        self._templates = templates
        self._overlays = overlays
        self._resolver = resolver
        self._event_bus = event_bus
        self._repo = evolution_repo

    async def propose(
        self,
        *,
        instance_id: str,
        tenant_id: str,
        user_id: str,
        proposed_overrides: dict,
        proposed_milestones: tuple[str, ...] = (),
        proposed_unlocked_skills: tuple[str, ...] = (),
        proposed_bond_summary: str | None = None,
        proposed_by: Literal["self", "emotion_service", "user_feedback", "admin"] = "self",
        rationale: str = "",
    ) -> EvolutionDelta:
        """Build a delta but do not apply. Caller decides approval flow."""
        overlay = self._overlays.load(tenant_id, user_id, instance_id)
        template = self._templates.get(overlay.template_id)
        new_overlay = self._compute_new_overlay(
            overlay,
            proposed_overrides=proposed_overrides,
            proposed_milestones=proposed_milestones,
            proposed_unlocked_skills=proposed_unlocked_skills,
            proposed_bond_summary=proposed_bond_summary,
        )
        self._guard(template, overlay, new_overlay)
        delta = EvolutionDelta(
            id=uuid.uuid4().hex,
            instance_id=instance_id,
            from_overlay_version=overlay.template_version,
            to_overlay_version=new_overlay.template_version,
            changed_fields=_diff(overlay, new_overlay),
            rationale=rationale,
            proposed_by=proposed_by,
            requires_human_approval=template.evolution_policy.require_human_approval_milestones
            and bool(proposed_milestones),
        )
        if self._event_bus is not None:
            await self._event_bus.publish(
                Event(
                    subject=Topics.evolution_proposed(instance_id),
                    payload=delta.model_dump(mode="json"),
                    source="persona.evolution",
                )
            )
        return delta

    async def apply(
        self,
        *,
        delta: EvolutionDelta,
        tenant_id: str,
        user_id: str,
        approved_by: str | None = None,
    ) -> EvolutionDelta:
        overlay = self._overlays.load(tenant_id, user_id, delta.instance_id)
        template = self._templates.get(overlay.template_id)
        # Re-apply changed fields onto a fresh overlay (so concurrent edits don't lose data).
        merged_overrides = dict(overlay.overrides or {})
        for path, (_old, new) in delta.changed_fields.items():
            if path.startswith("overrides."):
                key = path[len("overrides.") :]
                merged_overrides[key] = new
        new_overlay = overlay.model_copy(
            update={
                "overrides": merged_overrides,
                "milestones": tuple(
                    set(overlay.milestones)
                    | {x for x in _milestone_diff(delta) if isinstance(x, str)}
                ),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        # Final guard (in case templates changed between propose and apply).
        self._guard(template, overlay, new_overlay)
        self._overlays.save(
            new_overlay,
            tenant_id=tenant_id,
            user_id=user_id,
            reason=f"apply evolution {delta.id}",
        )
        applied = delta.model_copy(
            update={
                "applied_at": datetime.now(timezone.utc),
                "approved_by": approved_by,
            }
        )
        await self._resolver.invalidate(delta.instance_id)
        if self._repo is not None:
            await self._repo.record(applied)
        if self._event_bus is not None:
            await self._event_bus.publish(
                Event(
                    subject=Topics.evolution_applied(delta.instance_id),
                    payload=applied.model_dump(mode="json"),
                    source="persona.evolution",
                )
            )
        return applied

    # ---- Guard ---------------------------------------------------------------

    def _guard(
        self,
        template: PersonaTemplate,
        old: PersonaOverlay,
        new: PersonaOverlay,
    ) -> None:
        # 1. Locked fields
        locked = set(template.locked_fields)
        for key in (new.overrides or {}):
            if key in locked:
                raise EvolutionGuardError(
                    f"locked field {key!r} cannot be overridden by overlay"
                )
        # 2. Big5 step
        if "big5" in (new.overrides or {}):
            new_b5 = new.overrides["big5"]
            old_b5 = (old.overrides or {}).get("big5", {})
            step_max = template.evolution_policy.big5_step_max
            for k, v in new_b5.items():
                old_v = old_b5.get(k, getattr(template.big5_base, k, 0.5))
                if abs(float(v) - float(old_v)) > step_max:
                    raise EvolutionGuardError(
                        f"big5.{k} delta {abs(v - old_v):.3f} exceeds step_max {step_max}"
                    )
        # 3. Taboos cannot be removed (overlay can't shorten them).
        new_taboos = (new.overrides or {}).get("taboos")
        if new_taboos is not None:
            old_taboos = set(template.taboos) | set((old.overrides or {}).get("taboos", []))
            missing = old_taboos - set(new_taboos)
            if missing:
                raise EvolutionGuardError(f"taboos cannot be removed: {sorted(missing)}")

    # ---- Helpers -------------------------------------------------------------

    def _compute_new_overlay(
        self,
        base: PersonaOverlay,
        *,
        proposed_overrides: dict,
        proposed_milestones: tuple[str, ...],
        proposed_unlocked_skills: tuple[str, ...],
        proposed_bond_summary: str | None,
    ) -> PersonaOverlay:
        merged_overrides = dict(base.overrides or {})
        merged_overrides.update(proposed_overrides)
        return base.model_copy(
            update={
                "overrides": merged_overrides,
                "milestones": tuple(set(base.milestones) | set(proposed_milestones)),
                "unlocked_skills": tuple(
                    set(base.unlocked_skills) | set(proposed_unlocked_skills)
                ),
                "bond_history_summary": proposed_bond_summary
                if proposed_bond_summary is not None
                else base.bond_history_summary,
                "updated_at": datetime.now(timezone.utc),
            }
        )


def _diff(old: PersonaOverlay, new: PersonaOverlay) -> dict[str, tuple]:
    diff: dict[str, tuple] = {}
    old_o = old.overrides or {}
    new_o = new.overrides or {}
    for key in set(old_o) | set(new_o):
        if old_o.get(key) != new_o.get(key):
            diff[f"overrides.{key}"] = (old_o.get(key), new_o.get(key))
    if old.milestones != new.milestones:
        diff["milestones"] = (list(old.milestones), list(new.milestones))
    if old.unlocked_skills != new.unlocked_skills:
        diff["unlocked_skills"] = (list(old.unlocked_skills), list(new.unlocked_skills))
    if old.bond_history_summary != new.bond_history_summary:
        diff["bond_history_summary"] = (old.bond_history_summary, new.bond_history_summary)
    return diff


def _milestone_diff(delta: EvolutionDelta) -> tuple:
    pair = delta.changed_fields.get("milestones")
    if pair is None:
        return ()
    _old, new = pair
    return tuple(new or ())


__all__ = ["EvolutionPlanner"]
