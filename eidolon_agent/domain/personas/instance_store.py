"""Read and write per-user persona instance copies."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from eidolon_agent.core.errors import NotFoundError, ValidationError
from eidolon_agent.domain.personas.types import PersonaInstance, PersonaTemplate


class PersonaInstanceStore:
    def __init__(self, instances_dir: Path) -> None:
        self._dir = instances_dir

    def path_for(self, tenant_id: str, user_id: str, instance_id: str) -> Path:
        return self._dir / tenant_id / user_id / f"{instance_id}.yaml"

    def exists(self, tenant_id: str, user_id: str, instance_id: str) -> bool:
        return self.path_for(tenant_id, user_id, instance_id).exists()

    def load(self, tenant_id: str, user_id: str, instance_id: str) -> PersonaInstance:
        path = self.path_for(tenant_id, user_id, instance_id)
        if not path.exists():
            raise NotFoundError(f"persona instance not found: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return PersonaInstance(**data)
        except Exception as exc:
            raise ValidationError(f"{path}: instance schema error: {exc}") from exc

    def save(self, instance: PersonaInstance, *, reason: str = "") -> None:
        path = self.path_for(instance.tenant_id, instance.user_id, instance.instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = instance.model_dump(mode="json")
        path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    def create_from_template(
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
        self.save(instance, reason="create_from_template")
        return instance

