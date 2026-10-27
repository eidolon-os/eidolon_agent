"""Establish one authenticated Owner/Companion/Session runtime boundary."""

from __future__ import annotations

from dataclasses import dataclass, replace

from eidolon_agent.core.errors import PermissionDeniedError, ValidationError
from eidolon_agent.core.ports.runtime_authority import CompanionRuntimeAuthority
from eidolon_agent.core.types.companion_runtime import (
    CompanionRuntimeConfig,
    CompanionRuntimeFacts,
)
from eidolon_agent.domain.tools.builtin.emit_event import EmitEventTool
from eidolon_agent.domain.tools.builtin.submit_long_task import DELEGATE_TO_COWORKER_TOOL


@dataclass(frozen=True, slots=True)
class AuthorizedRuntimeSession:
    """Immutable authority facts and namespace constraints for one credential."""

    runtime: CompanionRuntimeFacts
    device_id: str | None
    session_id: str
    config: CompanionRuntimeConfig
    person_id: str | None = None
    identity_epoch: int | None = None

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
        person_id: str | None = None,
        identity_epoch: int | None = None,
    ) -> AuthorizedRuntimeSession:
        if (person_id is None) != (identity_epoch is None):
            raise PermissionDeniedError("incomplete personal runtime binding")
        if person_id is not None and (
            not person_id.strip() or type(identity_epoch) is not int or identity_epoch < 0
        ):
            raise PermissionDeniedError("invalid personal runtime binding")
        normalized_session_id = (session_id or "").strip()
        if not normalized_session_id:
            raise PermissionDeniedError("runtime token is not bound to a session")
        runtime = await self._authority.resolve(
            owner_id=owner_id,
            companion_id=companion_id,
        )
        if runtime.owner_id != owner_id or runtime.companion_id != companion_id:
            raise PermissionDeniedError("runtime authority returned facts outside token scope")
        if person_id is not None:
            realm_id = await self._authority.resolve_person_memory(
                owner_id=owner_id, person_id=person_id
            )
            if not realm_id:
                raise PermissionDeniedError("personal runtime has no memory realm")
            runtime = replace(runtime, memory_realm_id=realm_id)
        try:
            config = CompanionRuntimeConfig.from_authority(runtime.runtime_config)
        except ValueError as exc:
            raise ValidationError(f"invalid Companion runtime config: {exc}") from exc
        if person_id is not None:
            # Coworker results and arbitrary event subjects are not Person scoped.
            # Reuse both tool visibility and dispatch deny enforcement.
            config = replace(config, tool_deny=config.tool_deny | {
                DELEGATE_TO_COWORKER_TOOL, EmitEventTool.schema.name,
            })
        normalized_device_id = (device_id or "").strip() or None
        return AuthorizedRuntimeSession(
            runtime=runtime,
            person_id=person_id,
            identity_epoch=identity_epoch,
            device_id=normalized_device_id,
            session_id=normalized_session_id,
            config=config,
        )


__all__ = ["AuthorizedRuntimeSession", "RuntimeSessionAuthorizer"]
