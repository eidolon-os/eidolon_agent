"""Tool — a callable capability the LLM can invoke.

Tools are registered at process start (built-ins) or via plugins. Each tool
declares its JSON schema (for the LLM) and a set of permissions (for the
sandbox). The dispatcher runs side-effect-free tools in parallel and
side-effectful tools serially.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Permission(str, Enum):
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    NETWORK = "network"
    FILESYSTEM = "filesystem"
    SYSTEM = "system"
    USER_DATA = "user_data"


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """Static description of a tool. Suitable for inclusion in LLM tools array."""

    name: str
    description: str
    json_schema: dict  # parameter JSON Schema
    permissions: frozenset[Permission] = frozenset()
    timeout_s: float = 5.0
    side_effect: bool = False  # True → dispatcher serializes
    supports_dry_run: bool = False
    idempotency_key_template: str | None = None  # e.g. "${user_id}:${date}:${vendor}"

    def to_openai_function(self) -> dict:
        """Render to OpenAI / Anthropic-compatible function spec."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema,
            },
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A pending invocation from the LLM."""

    id: str  # provider-supplied call id (used to correlate result)
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of a tool call. Includes both success and error shapes."""

    call_id: str
    name: str
    ok: bool
    content: Any = None  # JSON-serializable on success
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: int = 0
    metadata: dict = field(default_factory=dict)
