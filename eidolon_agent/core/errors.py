"""Exception hierarchy.

A single root makes it easy for the gRPC and HTTP interceptors to map exceptions
to status codes uniformly. Subclasses carry just enough metadata to let the
transport layer produce a useful client error without leaking internals.
"""

from __future__ import annotations


class EidolonError(Exception):
    """Root of all eidolon-agent exceptions."""

    code: str = "eidolon.error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


# --- Auth / authorization -----------------------------------------------------


class AuthError(EidolonError):
    code = "eidolon.auth"


class UnauthenticatedError(AuthError):
    code = "eidolon.unauthenticated"


class PermissionDeniedError(AuthError):
    code = "eidolon.permission_denied"


class TokenRevokedError(AuthError):
    code = "eidolon.token_revoked"


# --- Resource lifecycle -------------------------------------------------------


class NotFoundError(EidolonError):
    code = "eidolon.not_found"


class ConflictError(EidolonError):
    code = "eidolon.conflict"


class QuotaExceededError(EidolonError):
    code = "eidolon.quota_exceeded"


class RateLimitedError(EidolonError):
    code = "eidolon.rate_limited"


# --- Configuration / validation ----------------------------------------------


class ConfigError(EidolonError):
    code = "eidolon.config"


class ValidationError(EidolonError):
    code = "eidolon.validation"


# --- Runtime / external dependencies -----------------------------------------


class DependencyError(EidolonError):
    """A required external dependency is unhealthy."""

    code = "eidolon.dependency"


class MemoryUnavailableError(DependencyError):
    code = "eidolon.memory_unavailable"


class LLMUnavailableError(DependencyError):
    code = "eidolon.llm_unavailable"


class NatsUnavailableError(DependencyError):
    code = "eidolon.nats_unavailable"


# --- Turn / streaming ---------------------------------------------------------


class TurnError(EidolonError):
    code = "eidolon.turn"


class TurnCancelledError(TurnError):
    code = "eidolon.turn_cancelled"


class TurnTimeoutError(TurnError):
    code = "eidolon.turn_timeout"


# --- Persona / evolution ------------------------------------------------------


class PersonaError(EidolonError):
    code = "eidolon.persona"


class EvolutionGuardError(PersonaError):
    code = "eidolon.evolution_guard"


# --- Tool execution -----------------------------------------------------------


class ToolError(EidolonError):
    code = "eidolon.tool"


class ToolPermissionError(ToolError):
    code = "eidolon.tool_permission_denied"


class ToolTimeoutError(ToolError):
    code = "eidolon.tool_timeout"


# --- Guardrails ---------------------------------------------------------------


class GuardrailBlockedError(EidolonError):
    """Pre-LLM guardrail rejected the input or post-LLM rejected the output."""

    code = "eidolon.guardrail_blocked"


__all__ = [
    "AuthError",
    "ConfigError",
    "ConflictError",
    "DependencyError",
    "EidolonError",
    "EvolutionGuardError",
    "GuardrailBlockedError",
    "LLMUnavailableError",
    "MemoryUnavailableError",
    "NatsUnavailableError",
    "NotFoundError",
    "PermissionDeniedError",
    "PersonaError",
    "QuotaExceededError",
    "RateLimitedError",
    "TokenRevokedError",
    "ToolError",
    "ToolPermissionError",
    "ToolTimeoutError",
    "TurnCancelledError",
    "TurnError",
    "TurnTimeoutError",
    "UnauthenticatedError",
    "ValidationError",
]
