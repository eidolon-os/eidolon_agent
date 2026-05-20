"""Persona = Template (genome) + Overlay (evolution layer).

- :class:`PersonaTemplateRegistry` loads/watches the immutable templates.
- :class:`PersonaOverlayStore` reads/writes per-instance overlays.
- :class:`PersonaResolver` deep-merges Template ⊕ Overlay → CompanionProfile.
- :class:`EvolutionPlanner` proposes, guards, applies, and rolls back overlay deltas.
"""

from eidolon_agent.persona.evolution import EvolutionPlanner
from eidolon_agent.persona.overlay_store import PersonaOverlayStore
from eidolon_agent.persona.resolver import PersonaResolver
from eidolon_agent.persona.template_registry import PersonaTemplateRegistry

__all__ = [
    "EvolutionPlanner",
    "PersonaOverlayStore",
    "PersonaResolver",
    "PersonaTemplateRegistry",
]
