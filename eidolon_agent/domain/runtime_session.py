"""Establish one authenticated Owner/Companion/Session runtime boundary."""

from __future__ import annotations

from dataclasses import dataclass

from eidolon_agent.core.errors import PermissionDeniedError, ValidationError
from eidolon_agent.core.ports.runtime_authority import CompanionRuntimeAuthority
from eidolon_agent.core.types.companion_runtime import (
    CompanionRuntimeConfig,
    CompanionRuntimeFacts,
)


@dataclass(frozen=True, slots=True)
class AuthorizedRuntimeSession:
    """Immutable authority facts and namespace constraints for one credential."""

    runtime: CompanionRuntimeFacts
    device_id: str | None
    session_id: str
    config: CompanionRuntimeConfig

    @property
    def owner_id(self) -> str:
        return self.runtime.owner_id

    @property
    def companion_id(self) -> str:
        return self.runtime.companion_id


class RuntimeSessionAuthorizer:
    """Resolve signed identity claims into a single fail-closed runtime scope."""

    def __init__(self, authority: CompanionRuntimeAuthority) -> None:
        self._authority = authority

    async def authorize(
        self,
        *,
        owner_id: str,
        companion_id: str,
        device_id: str | None,
        session_id: str | None,
    ) -> AuthorizedRuntimeSession:
        normalized_session_id = (session_id or "").strip()
        if not normalized_session_id:
            raise PermissionDeniedError("runtime token is not bound to a session")
        runtime = await self._authority.resolve(
            owner_id=owner_id,
            companion_id=companion_id,
        )
        if runtime.owner_id != owner_id or runtime.companion_id != companion_id:
            raise PermissionDeniedError("runtime authority returned facts outside token scope")
        try:
            config = CompanionRuntimeConfig.from_authority(runtime.runtime_config)
        except ValueError as exc:
            raise ValidationError(f"invalid Companion runtime config: {exc}") from exc
        normalized_device_id = (device_id or "").strip() or None
        return AuthorizedRuntimeSession(
            runtime=runtime,
            device_id=normalized_device_id,
            session_id=normalized_session_id,
            config=config,
        )


__all__ = ["AuthorizedRuntimeSession", "RuntimeSessionAuthorizer"]
