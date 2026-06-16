"""Centralized NATS subject names.

All subjects MUST be constructed via this module — no string literals scattered
across the codebase. This makes refactoring trivial and lets us enforce the
``agent.<scope>.<tenant>.<user>.<...>`` namespacing convention.
"""

from __future__ import annotations

class Topics:
    """Subject builders. All methods return strings; instances are stateless."""

    # --- Internal events ------------------------------------------------------

    @staticmethod
    def turn_completed(conv_id: str) -> str:
        return f"agent.turn.completed.{conv_id}"

    @staticmethod
    def fsm_changed(session_id: str) -> str:
        return f"agent.fsm.changed.{session_id}"

    @staticmethod
    def persona_template_reloaded(template_id: str) -> str:
        return f"agent.persona.template.reloaded.{template_id}"

    @staticmethod
    def persona_overlay_updated(instance_id: str) -> str:
        return f"agent.persona.overlay.updated.{instance_id}"

    @staticmethod
    def evolution_proposed(instance_id: str) -> str:
        return f"agent.evolution.proposed.{instance_id}"

    @staticmethod
    def evolution_applied(instance_id: str) -> str:
        return f"agent.evolution.applied.{instance_id}"

    @staticmethod
    def evolution_rolled_back(instance_id: str) -> str:
        return f"agent.evolution.rolled_back.{instance_id}"

    @staticmethod
    def proactive_triggered(instance_id: str) -> str:
        return f"agent.proactive.triggered.{instance_id}"

    @staticmethod
    def proactive_suppressed(instance_id: str) -> str:
        return f"agent.proactive.suppressed.{instance_id}"

    @staticmethod
    def signal(modality: str, session_id: str) -> str:
        return f"agent.signal.{modality}.{session_id}"

    @staticmethod
    def system_config_updated() -> str:
        return "agent.system.config.updated"

    @staticmethod
    def pairing_revoked() -> str:
        return "agent.pairing.revoked"

    # --- External outbound ----------------------------------------------------

    @staticmethod
    def emotion_turn(user_id: str) -> str:
        return f"agent.emotion.turn.{user_id}"

    # --- External inbound -----------------------------------------------------

    @staticmethod
    def memory_event_pattern() -> str:
        return "agent.memory.event.*"

    @staticmethod
    def emotion_proposed_pattern() -> str:
        return "agent.persona.evolution.proposed.*"

# JetStream-persistent subjects (these go through JetStream, others are core NATS).
PERSISTENT_PREFIXES = (
    "agent.memory.",
    "agent.emotion.",
    "agent.evolution.",
)


def is_persistent(subject: str) -> bool:
    """Whether a subject should be published through JetStream vs core NATS."""
    return any(subject.startswith(p) for p in PERSISTENT_PREFIXES)
