"""Ports — Protocol definitions for every external capability.

The hex-arch contract: business code depends only on these Protocols, never
on concrete adapters. Concrete adapters live under the corresponding feature
package (``eidolon_agent.memory``, ``eidolon_agent.brain.llm``, etc.).

Every Protocol is runtime-checkable (``@runtime_checkable``) so tests can
verify duck-typed fakes match.
"""

from __future__ import annotations

from eidolon_agent.core.ports.context import ContextProvider
from eidolon_agent.core.ports.dispatch import DispatchPort
from eidolon_agent.core.ports.events import EventBus, EventHandler, KVStore
from eidolon_agent.core.ports.hooks import (
    HookEvent,
    HookOutcome,
    HookPayload,
    HookPort,
    HookResult,
)
from eidolon_agent.core.ports.llm import LLMPort
from eidolon_agent.core.ports.memory import MemoryPort
from eidolon_agent.core.ports.persistence import (
    ChatMessageRepository,
    ConversationRepository,
    DeviceRepository,
    UnitOfWork,
)
from eidolon_agent.core.ports.tool import ToolPort

__all__ = [
    "ChatMessageRepository",
    "ContextProvider",
    "ConversationRepository",
    "DeviceRepository",
    "DispatchPort",
    "EventBus",
    "EventHandler",
    "HookEvent",
    "HookOutcome",
    "HookPayload",
    "HookPort",
    "HookResult",
    "KVStore",
    "LLMPort",
    "MemoryPort",
    "ToolPort",
    "UnitOfWork",
]
