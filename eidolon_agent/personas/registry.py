"""Template registry for canonical persona templates."""

from __future__ import annotations

from pathlib import Path

import yaml

from eidolon_agent.core.errors import NotFoundError, ValidationError
from eidolon_agent.personas.types import PersonaTemplate, PersonaTemplateSummary


class PersonaTemplateRegistry:
    def __init__(self, templates_dir: Path) -> None:
        self._dir = templates_dir
        self._templates: dict[str, PersonaTemplate] = {}

    async def load_all(self) -> None:
        self._templates.clear()
        if not self._dir.exists():
            return
        for path in sorted(self._dir.glob("*.yaml")):
            template = _parse_template(path)
            self._templates[template.metadata.template_id] = template

    def list_templates(self) -> list[PersonaTemplateSummary]:
        return [
            PersonaTemplateSummary(
                template_id=t.metadata.template_id,
                template_revision=t.metadata.template_revision,
                name=t.metadata.name,
                archetype=t.metadata.archetype,
                description=t.metadata.description,
            )
            for t in sorted(self._templates.values(), key=lambda item: item.metadata.template_id)
        ]

    def list_all(self) -> list[PersonaTemplate]:
        return [self._templates[k] for k in sorted(self._templates)]

    def get(self, template_id: str) -> PersonaTemplate:
        try:
            return self._templates[template_id]
        except KeyError as exc:
            raise NotFoundError(f"persona template not found: {template_id}") from exc


def _parse_template(path: Path) -> PersonaTemplate:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValidationError(f"{path}: YAML parse error: {exc}") from exc
    if "schema_version" in raw:
        raise ValidationError(f"{path}: schema_version is not used by canonical personas")
    try:
        return PersonaTemplate(**raw)
    except Exception as exc:
        raise ValidationError(f"{path}: template schema error: {exc}") from exc

