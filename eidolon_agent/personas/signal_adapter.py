"""Translate realtime signal digests into persona runtime state changes."""

from __future__ import annotations

from eidolon_agent.personas.types import AttentionTarget, PersonaSignalInput


class PersonaSignalAdapter:
    def to_runtime_update(self, signal: PersonaSignalInput) -> dict:
        if signal.confidence_overall < 0.6:
            return {}

        update: dict = {}
        if signal.dominant_emotion and signal.emotion_confidence >= 0.6:
            update["emotion"] = _normalize_emotion(signal.dominant_emotion)
            update["emotion_delta"] = min(0.35, max(0.05, signal.emotion_confidence * 0.25))
        if signal.presence == "away":
            update["attention_target"] = AttentionTarget.IDLE
            update["focus_score"] = 0.1
        elif signal.presence == "distracted":
            update["attention_target"] = AttentionTarget.DRIFT
            update["focus_score"] = 0.35
        else:
            update["attention_target"] = AttentionTarget.USER
            update["focus_score"] = 0.75
        return update


def _normalize_emotion(label: str) -> str:
    mapping = {
        "happy": "joy",
        "smile": "joy",
        "laugh": "joy",
        "sad": "sad",
        "angry": "anger",
        "anger": "anger",
        "fear": "fear",
        "anxious": "fear",
        "surprised": "surprise",
        "surprise": "surprise",
    }
    return mapping.get(label, label if label in {"joy", "sad", "anger", "fear", "surprise"} else "surprise")

