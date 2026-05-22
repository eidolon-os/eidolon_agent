"""Independent personas module.

Use :class:`PersonasService` as the public facade. Other classes are exported
for tests and local composition, but application modules should depend on the
service interface.
"""

from eidolon_agent.personas.compiler import PersonaCompiler
from eidolon_agent.personas.evolution import PersonaEvolutionEngine
from eidolon_agent.personas.instance_store import PersonaInstanceStore
from eidolon_agent.personas.memory_adapter import PersonaMemoryAdapter
from eidolon_agent.personas.registry import PersonaTemplateRegistry
from eidolon_agent.personas.runtime_state import PersonaRuntimeStateStore
from eidolon_agent.personas.service import PersonasService, build_default_personas_service
from eidolon_agent.personas.types import (
    Attention,
    AttentionTarget,
    CompiledPersona,
    Energy,
    MoodVector,
    PersonaEvolutionEvent,
    PersonaEvolutionResult,
    PersonaInstance,
    PersonaInteractionEvent,
    PersonaMockResult,
    PersonaProactiveDecision,
    PersonaRuntimeState,
    PersonaSignalInput,
    PersonaSnapshot,
    PersonaTemplate,
    PersonaTemplateSummary,
)

__all__ = [
    "Attention",
    "AttentionTarget",
    "CompiledPersona",
    "Energy",
    "MoodVector",
    "PersonaCompiler",
    "PersonaEvolutionEngine",
    "PersonaEvolutionEvent",
    "PersonaEvolutionResult",
    "PersonaInstance",
    "PersonaInstanceStore",
    "PersonaInteractionEvent",
    "PersonaMemoryAdapter",
    "PersonaMockResult",
    "PersonaProactiveDecision",
    "PersonaRuntimeState",
    "PersonaRuntimeStateStore",
    "PersonaSignalInput",
    "PersonaSnapshot",
    "PersonaTemplate",
    "PersonaTemplateRegistry",
    "PersonaTemplateSummary",
    "PersonasService",
    "build_default_personas_service",
]
