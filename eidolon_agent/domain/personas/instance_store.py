"""YAML-backed PersonaInstanceStore implementation.

Kept as the legacy / migration-source storage layer. Production uses
``SqlPersonaInstanceStore`` (see ``infra/persistence/sql_persona_instance_store``)
which provides atomic single-TX evolution writes; this YAML store remains so
the migration script and self-contained tests can read existing files.

The class is async on every method (returning fast since file I/O is small)
so it satisfies the ``PersonaInstanceStore`` Protocol shared with the SQL
implementation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import yaml

from eidolon_agent.core.errors import NotFoundError, ValidationError
from eidolon_agent.domain.personas.types import PersonaInstance, PersonaTemplate


class YamlPersonaInstanceStore:
    """One ``{instance_id}.yaml`` per instance under ``<root>/<tenant>/<user>/``.

    All public methods are async even though the actual file I/O is sync —
    keeping the surface uniform with the SQL store means service-layer code
    doesn't have to know which backend is in use. We wrap the small amount of
    sync work in ``asyncio.to_thread`` so we don't block the event loop on
    slow filesystems.
    """

    def __init__(self, instances_dir: Path) -> None:
        self._dir = instances_dir

    def path_for(self, tenant_id: str, user_id: str, instance_id: str) -> Path:
        return self._dir / tenant_id / user_id / f"{instance_id}.yaml"

    async def exists(self, tenant_id: str, user_id: str, instance_id: str) -> bool:
        return await asyncio.to_thread(
            self.path_for(tenant_id, user_id, instance_id).exists
        )

    async def load(
        self, tenant_id: str, user_id: str, instance_id: str
    ) -> PersonaInstance:
        path = self.path_for(tenant_id, user_id, instance_id)

        def _read() -> PersonaInstance:
            if not path.exists():
                raise NotFoundError(f"persona instance not found: {path}")
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                return PersonaInstance(**data)
            except NotFoundError:
                raise
            except Exception as exc:
                raise ValidationError(f"{path}: instance schema error: {exc}") from exc

        return await asyncio.to_thread(_read)

    async def save(self, instance: PersonaInstance, *, reason: str = "") -> None:
        path = self.path_for(instance.tenant_id, instance.user_id, instance.instance_id)
        data = instance.model_dump(mode="json")

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )

        await asyncio.to_thread(_write)

    async def create_from_template(
        self,
        *,
        template: PersonaTemplate,
        tenant_id: str,
        user_id: str,
        instance_id: str,
    ) -> PersonaInstance:
        now = datetime.now(timezone.utc)
        instance = PersonaInstance(
            instance_id=instance_id,
            tenant_id=tenant_id,
            user_id=user_id,
            origin_template_id=template.metadata.template_id,
            origin_template_revision=template.metadata.template_revision,
            overlay_version=1,
            created_at=now,
            updated_at=now,
            metadata=template.metadata,
            identity_core=template.identity_core,
            behavioral_knobs=template.behavioral_knobs,
            style_compiler=template.style_compiler,
            memory_adapter=template.memory_adapter,
            evolution_rules=template.evolution_rules,
            assets=template.assets,
        )
        await self.save(instance, reason="create_from_template")
        return instance

    async def list_all(self) -> list[PersonaInstance]:
        def _scan() -> list[Path]:
            if not self._dir.exists():
                return []
            return list(self._dir.rglob("*.yaml"))

        paths = await asyncio.to_thread(_scan)
        out: list[PersonaInstance] = []
        for p in paths:
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                out.append(PersonaInstance(**data))
            except Exception:
                continue
        return out

    async def delete(self, tenant_id: str, user_id: str, instance_id: str) -> None:
        path = self.path_for(tenant_id, user_id, instance_id)

        def _unlink() -> None:
            path.unlink(missing_ok=True)

        await asyncio.to_thread(_unlink)
