"""Conversation identity owned by the Agent domain.

The value is deliberately opaque.  A Channel may currently use a stable
transport correlation key, but persistence, admin APIs and consumers must not
parse it or guess how long a UUID would have been.  This bound is the Agent's
one answer for storage and transport validation.
"""

from __future__ import annotations

CONVERSATION_ID_MAX_LENGTH = 512


def validate_conversation_id(value: str) -> str:
    """Return a valid opaque id, or reject it at the transport boundary."""

    if not value or not value.strip():
        raise ValueError("conversation_id must not be empty")
    if len(value) > CONVERSATION_ID_MAX_LENGTH:
        raise ValueError(f"conversation_id exceeds {CONVERSATION_ID_MAX_LENGTH} characters")
    return value
