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
    source_session_id: str | None = None
    person_revision: int | None = None
    identity_epoch: int | None = None
    memory_mode: str = "personal"
    provisional_memory_save: bool = False

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
        person_revision: int | None = None,
        source_session_id: str | None = None,
        memory_mode: str = "personal",
    ) -> AuthorizedRuntimeSession:
        if (person_id is None) != (identity_epoch is None):
            raise PermissionDeniedError("incomplete personal runtime binding")
        if person_id is not None and (
            not person_id.strip() or type(identity_epoch) is not int or identity_epoch < 0
        ):
            raise PermissionDeniedError("invalid personal runtime binding")
        if person_id is not None and (type(person_revision) is not int or person_revision < 1):
            raise PermissionDeniedError("personal runtime requires person revision")
        if person_id is None and person_revision is not None:
            raise PermissionDeniedError("person revision requires personal binding")
        normalized_session_id = (session_id or "").strip()
        if not normalized_session_id:
            raise PermissionDeniedError("runtime token is not bound to a session")
        if memory_mode not in ("personal", "provisional"):
            raise PermissionDeniedError("unsupported runtime memory mode")
        if memory_mode == "provisional" and (
            person_id is None or source_session_id != normalized_session_id
        ):
            raise PermissionDeniedError("provisional runtime requires the current source session")
        provisional_save = False
        runtime = await self._authority.resolve(
            owner_id=owner_id,
            companion_id=companion_id,
        )
        if runtime.owner_id != owner_id or runtime.companion_id != companion_id:
            raise PermissionDeniedError("runtime authority returned facts outside token scope")
        if memory_mode == "provisional":
            binding = await self._authority.resolve_provisional_memory(
                owner_id=owner_id, person_id=person_id, person_revision=person_revision,
                session_id=normalized_session_id)
            if (binding.owner_id, binding.person_id, binding.person_revision, binding.session_id) != (
                owner_id, person_id, person_revision, normalized_session_id,
            ):
                raise PermissionDeniedError("provisional authority outside token scope")
            if not binding.policy.save_allowed:
                raise PermissionDeniedError("provisional save is not permitted")
            provisional_save = True
            runtime = replace(runtime, memory_realm_id=binding.realm_id)
        elif person_id is not None:
            realm_id = await self._authority.resolve_person_memory(
                owner_id=owner_id, person_id=person_id, person_revision=person_revision
            )
            if not realm_id:
                raise PermissionDeniedError("personal runtime has no memory realm")
            runtime = replace(runtime, memory_realm_id=realm_id)
        try:
            config = CompanionRuntimeConfig.from_authority(runtime.runtime_config)
        except ValueError as exc:
            raise ValidationError(f"invalid Companion runtime config: {exc}") from exc
        # Interactive sessions must not publish through Owner-wide side channels,
        # whether their current Person is identified or unknown.
        config = replace(config, tool_deny=config.tool_deny | {
            DELEGATE_TO_COWORKER_TOOL, EmitEventTool.schema.name,
        })
        normalized_device_id = (device_id or "").strip() or None
        return AuthorizedRuntimeSession(
            runtime=runtime,
            memory_mode=memory_mode,
            provisional_memory_save=provisional_save,
            person_id=person_id,
            person_revision=person_revision,
            source_session_id=source_session_id,
            identity_epoch=identity_epoch,
            device_id=normalized_device_id,
            session_id=normalized_session_id,
            config=config,
        )


__all__ = ["AuthorizedRuntimeSession", "RuntimeSessionAuthorizer"]
