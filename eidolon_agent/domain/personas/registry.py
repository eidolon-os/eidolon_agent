"""Template registry for canonical persona templates.

Two sources at the same logical address:

  - **builtin**: YAML files under ``templates_dir``, loaded once at
    bootstrap. Read-only — these are deployment artifacts shipped with
    the agent. Cached in memory because the set is small and static
    until the process restarts.

  - **custom**: operator-authored templates persisted as Eidolon Data
    event-backed custom template store. The registry
    holds an in-memory cache of the custom set, refreshed whenever admin CRUD
    mutates it.

Read API stays **synchronous** — registry consumers (turn compilation,
prompt rendering, evolution) need a fast lookup with no SQL roundtrip
on the hot path. Only the cache-refresh side effect of admin writes is
async.

When both sources hold the same ``template_id`` the custom version
wins (operator's hand-edit overrides the shipped baseline).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

import yaml

from eidolon_agent.core.errors import NotFoundError, ValidationError
from eidolon_agent.domain.personas.types import PersonaTemplate, PersonaTemplateSummary

_log = logging.getLogger(__name__)


class _CustomTemplateSource(Protocol):
    """The minimal slice of ``SqlCustomTemplateStore`` the registry needs.

    Declared as a Protocol so the domain layer keeps its
    ``no-infra-import`` rule (the actual SQL store lives in
    ``infra/persistence``).
    """

    async def list_all(self): ...  # returns list[CustomTemplateView]


class PersonaTemplateRegistry:
    """Two-source registry. Read API is sync; cache refresh is async."""

    def __init__(
        self,
        templates_dir: Path,
        *,
        custom_source: _CustomTemplateSource | None = None,
    ) -> None:
        self._dir = templates_dir
        # Builtin (immutable until process restart).
        self._builtin: dict[str, PersonaTemplate] = {}
        self._builtin_raw: dict[str, str] = {}
        # Custom (refreshed on admin CRUD).
        self._custom: dict[str, PersonaTemplate] = {}
        self._custom_raw: dict[str, str] = {}
        # Unparseable custom rows kept here for the list view to show
        # so the operator can find and delete broken yaml.
        self._custom_broken: dict[str, PersonaTemplateSummary] = {}
        self._custom_source = custom_source

    # ---- bootstrap / refresh ------------------------------------------------

    async def load_all(self) -> None:
        """Load both sources from scratch. Called once at boot."""
        self._load_builtin_from_disk()
        await self.refresh_custom()

    def _load_builtin_from_disk(self) -> None:
        self._builtin.clear()
        self._builtin_raw.clear()
        if not self._dir.exists():
            return
        for path in sorted(self._dir.glob("*.yaml")):
            text = path.read_text(encoding="utf-8")
            template = _parse_yaml_str_with_path_hint(text, path)
            self._builtin[template.metadata.template_id] = template
            self._builtin_raw[template.metadata.template_id] = text

    async def refresh_custom(self) -> None:
        """Reload the custom cache from the source. Called by orchestrator
        after any CRUD mutation. No-op when no custom_source is configured
        (e.g. tests that only need builtin).

        Important: the cache key is the STORE ROW's ``template_id`` (the
        SQL PK) — NOT the ``metadata.template_id`` embedded in the yaml.
        Fork copies a source's yaml verbatim under a new row id, so the
        yaml inside still references the OLD id. If we keyed by the
        embedded id, the fork would silently overwrite the source in the
        cache (one entry, source's id) — admin would see "only one
        template" after forking.

        Same care applies to the parsed PersonaTemplate's metadata —
        we override ``template_id`` (and ``template_revision``) to match
        the row so the rendered soul references the row's id, which is
        the only id the operator and downstream agents/instances use.
        """
        self._custom.clear()
        self._custom_raw.clear()
        self._custom_broken.clear()
        if self._custom_source is None:
            return
        rows = await self._custom_source.list_all()
        for row in rows:
            try:
                parsed = _parse_yaml_str(row.yaml_body)
                # Override the parsed metadata so the row's identity is
                # the source of truth, not whatever the yaml text says:
                #   - template_id: the SQL PK (fork copies yaml verbatim
                #     under a new row id; without override the cache key
                #     would collide with the source)
                #   - template_revision: bumps on every PUT, the parser
                #     can't know that
                #   - name: the row's operator-chosen display_name
                #     (operator picked it via "display_name" in the form;
                #     the yaml's metadata.name is whatever was in the
                #     forked source and may be misleading for an edit)
                normalized = parsed.model_copy(
                    update={
                        "metadata": parsed.metadata.model_copy(
                            update={
                                "template_id": row.template_id,
                                "template_revision": row.revision,
                                "name": row.display_name,
                            }
                        )
                    }
                )
                self._custom[row.template_id] = normalized
                self._custom_raw[row.template_id] = row.yaml_body
            except ValidationError as exc:
                _log.warning(
                    "custom template %r unparseable; surfacing as broken: %s",
                    row.template_id,
                    exc,
                )
                # Fall back to denormalised display name so the operator
                # can still see + delete it from the list.
                self._custom_broken[row.template_id] = PersonaTemplateSummary(
                    template_id=row.template_id,
                    template_revision=row.revision,
                    name=row.display_name,
                    archetype=row.archetype,
                    description="(unparseable YAML — please edit or delete)",
                )

    # ---- read API (sync, custom-aware) -------------------------------------

    def list_templates(self) -> list[PersonaTemplateSummary]:
        """Builtin + custom (+ broken-custom placeholders). Custom wins
        on id collision. Stable order: by template_id."""
        merged: dict[str, PersonaTemplateSummary] = {
            tid: PersonaTemplateSummary(
                template_id=t.metadata.template_id,
                template_revision=t.metadata.template_revision,
                name=t.metadata.name,
                archetype=t.metadata.archetype,
                description=t.metadata.description,
            )
            for tid, t in self._builtin.items()
        }
        for tid, t in self._custom.items():
            merged[tid] = PersonaTemplateSummary(
                template_id=t.metadata.template_id,
                template_revision=t.metadata.template_revision,
                name=t.metadata.name,
                archetype=t.metadata.archetype,
                description=t.metadata.description,
            )
        # broken rows surface so operator can delete them
        for tid, summary in self._custom_broken.items():
            if tid not in merged:  # don't overwrite a parseable copy
                merged[tid] = summary
        return sorted(merged.values(), key=lambda s: s.template_id)

    def list_all(self) -> list[PersonaTemplate]:
        """Parsed objects only (builtin + parseable custom)."""
        out: dict[str, PersonaTemplate] = dict(self._builtin)
        out.update(self._custom)  # custom wins on collision
        return [out[k] for k in sorted(out)]

    def get(self, template_id: str) -> PersonaTemplate:
        """Custom wins over builtin on id collision."""
        if template_id in self._custom:
            return self._custom[template_id]
        try:
            return self._builtin[template_id]
        except KeyError as exc:
            raise NotFoundError(
                f"persona template not found: {template_id}"
            ) from exc

    def raw_yaml(self, template_id: str) -> str:
        """Original YAML for the admin source view. Custom > builtin."""
        if template_id in self._custom_raw:
            return self._custom_raw[template_id]
        raw = self._builtin_raw.get(template_id)
        if raw is None:
            raise NotFoundError(f"persona template not found: {template_id}")
        return raw

    # ---- introspection ------------------------------------------------------

    def builtin_ids(self) -> set[str]:
        """Used by admin endpoints to decide whether DELETE is permitted
        (builtin templates can't be deleted — only forked)."""
        return set(self._builtin.keys())

    def is_custom(self, template_id: str) -> bool:
        return template_id in self._custom or template_id in self._custom_broken


def _parse_yaml_str_with_path_hint(text: str, path: Path) -> PersonaTemplate:
    try:
        return _parse_yaml_str(text)
    except ValidationError as exc:
        raise ValidationError(f"{path}: {exc}") from exc


def _parse_yaml_str(text: str) -> PersonaTemplate:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValidationError(f"YAML parse error: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationError(
            "template root must be a mapping, got " + type(raw).__name__
        )
    if "schema_version" in raw:
        raise ValidationError("schema_version is not used by canonical personas")
    try:
        return PersonaTemplate(**raw)
    except Exception as exc:
        raise ValidationError(f"template schema error: {exc}") from exc
