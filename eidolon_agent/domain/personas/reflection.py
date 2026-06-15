"""Observation aggregation for long-term persona evolution."""

from __future__ import annotations

import uuid
from collections import defaultdict

from eidolon_agent.domain.personas.types import (
    PersonaEvolutionProposal,
    PersonaInstance,
    PersonaObservation,
    PersonaProposalPatch,
)


class PersonaReflectionEngine:
    """Turn durable observations into narrow, reviewable proposals.

    This first pass is deterministic on purpose. It keeps the business rules in
    the persona domain and avoids introducing an LLM dependency into the admin
    control path. Future versions can add an LLM-backed proposer behind this
    same interface.
    """

    def reflect(
        self,
        *,
        instance: PersonaInstance,
        observations: list[PersonaObservation],
        limit: int = 50,
    ) -> list[PersonaEvolutionProposal]:
        active = [
            obs
            for obs in observations[:limit]
            if obs.status == "active" and obs.confidence >= 0.45 and obs.strength >= 0.35
        ]
        grouped: dict[str, list[PersonaObservation]] = defaultdict(list)
        for obs in active:
            grouped[obs.kind].append(obs)

        proposals: list[PersonaEvolutionProposal] = []
        for kind, items in grouped.items():
            patches = self._patches_for_kind(instance=instance, kind=kind)
            if not patches:
                continue
            confidence = min(0.95, sum(obs.confidence for obs in items) / len(items))
            proposals.append(
                PersonaEvolutionProposal(
                    id=f"proposal-{uuid.uuid4().hex}",
                    tenant_id=instance.tenant_id,
                    user_id=instance.user_id,
                    instance_id=instance.instance_id,
                    patches=tuple(patches),
                    confidence=confidence,
                    rationale=_rationale_for_kind(kind),
                    evidence_ids=tuple(obs.id for obs in items),
                )
            )
        return proposals

    def _patches_for_kind(
        self, *, instance: PersonaInstance, kind: str
    ) -> list[PersonaProposalPatch]:
        def knob(name: str, delta: float, rationale: str) -> PersonaProposalPatch | None:
            if name not in instance.behavioral_knobs:
                return None
            return PersonaProposalPatch(
                type="knob_delta",
                target=f"behavioral_knobs.{name}",
                delta=delta,
                rationale=rationale,
            )

        if kind == "positive_feedback_received":
            patch = knob("intimacy", 0.03, "用户正向反馈说明当前亲近度可小幅增加。")
            return [patch] if patch else []
        if kind in {"user_requests_less_advice", "boundary_correction_received"}:
            patch = knob("directiveness", -0.04, "用户表达边界或少建议偏好，应降低推进感。")
            if patch:
                return [patch]
            patch = knob("extraversion", -0.03, "用户表达边界或少建议偏好，应减少主动延展。")
            return [patch] if patch else []
        if kind == "goal_progress_shared":
            patch = knob("structure", 0.04, "用户持续推进目标，结构化陪伴可稍微增强。")
            if patch:
                return [patch]
            patch = knob("directiveness", 0.03, "用户持续推进目标，可轻微增强行动陪伴。")
            return [patch] if patch else []
        if kind == "creative_project_recalled":
            patch = knob("imagination", 0.04, "创作相关记忆被反复使用，想象力响应可更充分。")
            if patch:
                return [patch]
            patch = knob("playfulness", 0.03, "创作相关记忆被反复使用，可增加轻盈联想。")
            return [patch] if patch else []
        if kind == "stressor_memory_recalled":
            patch = knob("grounding", 0.04, "压力相关记忆被命中，应增强稳定承接和落地感。")
            if patch:
                return [patch]
            patches = [
                item
                for item in (
                    knob("intimacy", 0.02, "压力场景下可更贴近地承接。"),
                    knob("extraversion", -0.02, "压力场景下应减少外放和打岔。"),
                )
                if item is not None
            ]
            return patches
        return []


def _rationale_for_kind(kind: str) -> str:
    return {
        "positive_feedback_received": "正向反馈累计，建议小幅贴近。",
        "user_requests_less_advice": "用户偏好更少建议，建议降低推进感。",
        "boundary_correction_received": "用户修正了互动边界，建议尊重边界并降低主动性。",
        "goal_progress_shared": "用户分享目标推进，建议增加结构化陪伴。",
        "creative_project_recalled": "创作项目记忆被命中，建议提高想象力响应。",
        "stressor_memory_recalled": "压力相关记忆被命中，建议增强稳定承接。",
    }.get(kind, f"根据 {kind} 观测生成的受控演化建议。")


__all__ = ["PersonaReflectionEngine"]
