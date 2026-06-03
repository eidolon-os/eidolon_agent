"""Runtime privacy policy for a single brain turn.

This module owns the decision logic. ``PrivacyTrace`` remains the persisted
metadata shape that admin/replay tools can display.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eidolon_agent.core.types import PrivacyTrace


@dataclass(frozen=True, slots=True)
class TurnRuntimePolicy:
    privacy: PrivacyTrace

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any]) -> "TurnRuntimePolicy":
        if metadata.get("temporary") or metadata.get("temporary_mode"):
            return cls(
                PrivacyTrace(
                    mode="temporary",
                    memory_recall_allowed=False,
                    memory_write_allowed=False,
                    history_visible_to_context=False,
                )
            )
        if metadata.get("is_private") or metadata.get("private"):
            return cls(
                PrivacyTrace(
                    mode="private",
                    memory_recall_allowed=True,
                    memory_write_allowed=False,
                    history_visible_to_context=False,
                )
            )
        return cls(PrivacyTrace())

    @property
    def memory_recall_allowed(self) -> bool:
        return self.privacy.memory_recall_allowed

    @property
    def memory_write_allowed(self) -> bool:
        return self.privacy.memory_write_allowed

    @property
    def history_context_allowed(self) -> bool:
        return self.privacy.history_visible_to_context

    @property
    def mark_messages_private(self) -> bool:
        return self.privacy.mode != "normal"

    @property
    def post_turn_side_effects_allowed(self) -> bool:
        return self.memory_write_allowed


__all__ = ["TurnRuntimePolicy"]
