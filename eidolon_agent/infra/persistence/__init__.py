"""Eidolon Agent persistence adapters.

Durable business data is owned by ``eidolon_data``. This package exposes only
the agent-side adapters that map agent domain types onto that unified schema.
"""

from eidolon_agent.infra.persistence.custom_template_types import (
    CustomTemplateAlreadyExists,
    CustomTemplateError,
    CustomTemplateInUse,
    CustomTemplateNotFound,
    CustomTemplateView,
)
from eidolon_agent.infra.persistence.eidolon_data_persona import (
    EidolonDataCustomTemplateStore,
    EidolonDataEvolutionHistoryStore,
    EidolonDataPersonaEvolutionProposalStore,
    EidolonDataPersonaInstanceStore,
    EidolonDataPersonaObservationStore,
)
from eidolon_agent.infra.persistence.eidolon_data_runtime import (
    EidolonDataConversationReader,
    EidolonDataLongTaskStore,
    EidolonDataMemoryFanoutStatusSink,
    build_eidolon_data_history_hydrator,
    build_eidolon_data_turn_persister,
)

__all__ = [
    "CustomTemplateAlreadyExists",
    "CustomTemplateError",
    "CustomTemplateInUse",
    "CustomTemplateNotFound",
    "CustomTemplateView",
    "EidolonDataConversationReader",
    "EidolonDataCustomTemplateStore",
    "EidolonDataEvolutionHistoryStore",
    "EidolonDataLongTaskStore",
    "EidolonDataMemoryFanoutStatusSink",
    "EidolonDataPersonaEvolutionProposalStore",
    "EidolonDataPersonaInstanceStore",
    "EidolonDataPersonaObservationStore",
    "build_eidolon_data_history_hydrator",
    "build_eidolon_data_turn_persister",
]
