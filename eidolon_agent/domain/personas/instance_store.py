"""YAML-backed CompanionPersonaStore implementation.

Kept for local debugging and self-contained tests. Production uses the
Eidolon Data persona adapter, which stores companion persona state as
``persona_genomes``.

The class is async on every method (returning fast since file I/O is small)
so it satisfies the ``CompanionPersonaStore`` protocol shared by persistence
adapters.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import yaml

from eidolon_agent.core.errors import NotFoundError, ValidationError
from eidolon_agent.domain.personas.types import CompanionPersona, PersonaTemplate


class YamlCompanionPersonaStore:
    """One ``{companion_id}.yaml`` per companion under ``<root>/<owner>/``.

    All public methods are async even though the actual file I/O is sync —
    keeping the surface uniform with the SQL store means service-layer code
    doesn't have to know which backend is in use. We wrap the small amount of
    sync work in ``asyncio.to_thread`` so we don't block the event loop on
    slow filesystems.
    """

    def __init__(self, instances_dir: Path) -> None:
        self._dir = instances_dir

    def path_for(self, owner_id: str, companion_id: str) -> Path:
        return self._dir / owner_id / f"{companion_id}.yaml"

    async def exists(self, owner_id: str, companion_id: str) -> bool:
        return await asyncio.to_thread(self.path_for(owner_id, companion_id).exists)

    async def load(self, owner_id: str, companion_id: str) -> CompanionPersona:
        path = self.path_for(owner_id, companion_id)

        def _read() -> CompanionPersona:
            if not path.exists():
                raise NotFoundError(f"companion persona not found: {path}")
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                return CompanionPersona(**data)
            except NotFoundError:
                raise
            except Exception as exc:
                raise ValidationError(f"{path}: persona schema error: {exc}") from exc

        return await asyncio.to_thread(_read)

    async def save(self, persona: CompanionPersona, *, reason: str = "") -> None:
        path = self.path_for(persona.owner_id, persona.companion_id)
        data = persona.model_dump(mode="json")

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
        owner_id: str,
        companion_id: str,
    ) -> CompanionPersona:
        now = datetime.now(timezone.utc)
        persona = CompanionPersona(
            companion_id=companion_id,
            owner_id=owner_id,
            origin_template_id=template.metadata.template_id,
            origin_template_revision=template.metadata.template_revision,
            version=1,
            created_at=now,
            updated_at=now,
            metadata=template.metadata,
            identity_core=template.identity_core,
            behavioral_knobs=template.behavioral_knobs,
            style_compiler=template.style_compiler,
            memory_adapter=template.memory_adapter,
            evolution_rules=template.evolution_rules,
            assets=template.assets,
            # Seed blueprint components; owner-specific ones authored per companion.
            example_dialogs=template.example_dialogs,
            goals=template.goals,
        )
        await self.save(persona, reason="create_from_template")
        return persona

    async def list_all(self) -> list[CompanionPersona]:
        def _scan() -> list[Path]:
            if not self._dir.exists():
                return []
            return list(self._dir.rglob("*.yaml"))

        paths = await asyncio.to_thread(_scan)
        out: list[CompanionPersona] = []
        for p in paths:
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                out.append(CompanionPersona(**data))
            except Exception:
                continue
        return out

    async def delete(self, owner_id: str, companion_id: str) -> None:
        path = self.path_for(owner_id, companion_id)

        def _unlink() -> None:
            path.unlink(missing_ok=True)

        await asyncio.to_thread(_unlink)
