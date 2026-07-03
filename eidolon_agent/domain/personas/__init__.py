"""Independent personas module.

Use :class:`PersonasService` as the public facade. Other classes are exported
for tests and local composition, but application modules should depend on the
service interface.
"""

from eidolon_agent.domain.personas.auto_evolution import PersonaAutoEvolutionPolicy
from eidolon_agent.domain.personas.compiler import PersonaCompiler
from eidolon_agent.domain.personas.evolution import PersonaEvolutionEngine
from eidolon_agent.domain.personas.instance_store import YamlCompanionPersonaStore
from eidolon_agent.domain.personas.memory_adapter import PersonaMemoryAdapter
from eidolon_agent.domain.personas.ports import CompanionPersonaStore
from eidolon_agent.domain.personas.reflection import PersonaReflectionEngine
from eidolon_agent.domain.personas.registry import PersonaTemplateRegistry
from eidolon_agent.domain.personas.runtime_state import PersonaRuntimeStateStore
from eidolon_agent.domain.personas.service import PersonasService
from eidolon_agent.domain.personas.template_renderer import render_template_markdown
from eidolon_agent.domain.personas.voice import (
    PersonaCard,
    PersonaVoice,
    card_from_persona,
)
from eidolon_agent.domain.personas.types import (
    Attention,
    AttentionTarget,
    CompanionPersona,
    CompiledPersona,
    Energy,
    MoodVector,
    PersonaEvolutionEvent,
    PersonaEvolutionProposal,
    PersonaEvolutionResult,
    PersonaInteractionEvent,
    PersonaMockResult,
    PersonaObservation,
    PersonaProactiveDecision,
    PersonaProposalPatch,
    PersonaRuntimeState,
    PersonaSignalInput,
    PersonaSnapshot,
    PersonaTemplate,
    PersonaTemplateSummary,
)

__all__ = [
    "Attention",
    "AttentionTarget",
    "CompanionPersona",
    "CompanionPersonaStore",
    "CompiledPersona",
    "Energy",
    "MoodVector",
    "PersonaAutoEvolutionPolicy",
    "PersonaCard",
    "PersonaCompiler",
    "PersonaEvolutionEngine",
    "PersonaEvolutionEvent",
    "PersonaEvolutionProposal",
    "PersonaEvolutionResult",
    "PersonaInteractionEvent",
    "PersonaMemoryAdapter",
    "PersonaMockResult",
    "PersonaObservation",
    "PersonaProactiveDecision",
    "PersonaProposalPatch",
    "PersonaReflectionEngine",
    "PersonaRuntimeState",
    "PersonaRuntimeStateStore",
    "PersonaSignalInput",
    "PersonaSnapshot",
    "PersonaTemplate",
    "PersonaTemplateRegistry",
    "PersonaTemplateSummary",
    "PersonaVoice",
    "PersonasService",
    "YamlCompanionPersonaStore",
    "card_from_persona",
    "render_template_markdown",
]
