"""Read/write per-instance PersonaOverlay YAML.

Overlays are evolution products — they accumulate the relationship between a
user and a specific AgentInstance. Path layout::

    personas/overlays/<tenant>/<user>/<instance_id>.yaml

Reads are synchronous (cheap) and don't go through KV cache — the
PersonaResolver caches.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from eidolon_agent.core.errors import NotFoundError, ValidationError
from eidolon_agent.core.types.persona import PersonaOverlay

_log = logging.getLogger(__name__)


class PersonaOverlayStore:
    def __init__(self, overlays_dir: Path) -> None:
        self._dir = overlays_dir

    def _path(self, tenant_id: str, user_id: str, instance_id: str) -> Path:
        return self._dir / tenant_id / user_id / f"{instance_id}.yaml"

    def exists(self, tenant_id: str, user_id: str, instance_id: str) -> bool:
        return self._path(tenant_id, user_id, instance_id).exists()

    def load(self, tenant_id: str, user_id: str, instance_id: str) -> PersonaOverlay:
        path = self._path(tenant_id, user_id, instance_id)
        if not path.exists():
            raise NotFoundError(f"overlay not found: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return PersonaOverlay(**data)
        except Exception as exc:
            raise ValidationError(f"{path}: overlay schema error: {exc}") from exc

    def save(self, overlay: PersonaOverlay, *, tenant_id: str, user_id: str, reason: str) -> None:
        path = self._path(tenant_id, user_id, overlay.instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = overlay.model_dump(mode="json")
        path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        _log.info("wrote overlay %s (%s)", path, reason)

    def init_empty(
        self,
        *,
        instance_id: str,
        template_id: str,
        template_version: int,
        tenant_id: str,
        user_id: str,
    ) -> PersonaOverlay:
        """Create and persist a fresh, empty overlay for a new AgentInstance."""
        now = datetime.now(timezone.utc)
        overlay = PersonaOverlay(
            instance_id=instance_id,
            template_id=template_id,
            template_version=template_version,
            created_at=now,
            updated_at=now,
        )
        self.save(overlay, tenant_id=tenant_id, user_id=user_id, reason="init")
        return overlay
