"""Load + watch PersonaTemplate YAML files.

Templates are the *genome*: read-only at runtime, version-controlled via Git.
The registry is the single source of truth for "what templates exist". File
system changes trigger ``TemplateReloaded`` events on the EventBus so live
instances can re-resolve their CompanionProfile.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import yaml
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from eidolon_agent.core.errors import NotFoundError, ValidationError
from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.persona import PersonaTemplate
from eidolon_agent.events.topics import Topics

_log = logging.getLogger(__name__)


class PersonaTemplateRegistry:
    """In-memory registry of all templates under :attr:`templates_dir`."""

    def __init__(
        self,
        templates_dir: Path,
        *,
        event_bus=None,  # EventBus protocol; optional for tests
        watch: bool = True,
    ) -> None:
        self._dir = templates_dir
        self._event_bus = event_bus
        self._watch_enabled = watch
        self._lock = asyncio.Lock()
        self._templates: dict[str, PersonaTemplate] = {}
        self._observer: Observer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- Loading -------------------------------------------------------------

    async def load_all(self) -> None:
        """Scan the templates directory and load every ``*.yaml`` file."""
        async with self._lock:
            self._templates.clear()
            if not self._dir.exists():
                _log.warning("templates dir %s does not exist; registry is empty", self._dir)
                return
            for path in sorted(self._dir.glob("*.yaml")):
                tpl = _parse_template_file(path)
                self._templates[tpl.template_id] = tpl
                _log.info("loaded persona template %s v%d", tpl.template_id, tpl.version)

    async def start(self) -> None:
        await self.load_all()
        if self._watch_enabled and self._dir.exists():
            self._loop = asyncio.get_running_loop()
            self._observer = Observer()
            self._observer.schedule(
                _TemplateWatchHandler(self), str(self._dir), recursive=False
            )
            self._observer.start()
            _log.info("watching templates dir %s", self._dir)

    async def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None

    # ---- Query ---------------------------------------------------------------

    def get(self, template_id: str) -> PersonaTemplate:
        try:
            return self._templates[template_id]
        except KeyError as exc:
            raise NotFoundError(f"persona template not found: {template_id}") from exc

    def list_ids(self) -> list[str]:
        return sorted(self._templates.keys())

    def list_all(self) -> list[PersonaTemplate]:
        return [self._templates[k] for k in sorted(self._templates)]

    # ---- Internal: reload on FS change --------------------------------------

    def _on_change(self, path: Path) -> None:
        if path.suffix != ".yaml":
            return
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._reload_one(path), self._loop)

    async def _reload_one(self, path: Path) -> None:
        async with self._lock:
            if not path.exists():
                # Deletion — drop matching template_id (best-effort by stem).
                stem = path.stem
                for tid in list(self._templates):
                    if tid == stem:
                        del self._templates[tid]
                        await self._emit_reload(tid)
                return
            try:
                tpl = _parse_template_file(path)
            except ValidationError as exc:
                _log.error("template reload failed for %s: %s", path, exc)
                return
            self._templates[tpl.template_id] = tpl
            await self._emit_reload(tpl.template_id)
            _log.info("reloaded template %s v%d", tpl.template_id, tpl.version)

    async def _emit_reload(self, template_id: str) -> None:
        if self._event_bus is None:
            return
        try:
            await self._event_bus.publish(
                Event(
                    subject=Topics.persona_template_reloaded(template_id),
                    payload={"template_id": template_id},
                    source="persona.template_registry",
                )
            )
        except Exception:
            _log.exception("emit TemplateReloaded failed")


def _parse_template_file(path: Path) -> PersonaTemplate:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValidationError(f"{path}: YAML parse error: {exc}") from exc
    try:
        return PersonaTemplate(**raw)
    except Exception as exc:
        raise ValidationError(f"{path}: template schema error: {exc}") from exc


class _TemplateWatchHandler(FileSystemEventHandler):
    def __init__(self, registry: PersonaTemplateRegistry) -> None:
        self._registry = registry

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._registry._on_change(Path(event.src_path))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._registry._on_change(Path(event.src_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._registry._on_change(Path(event.src_path))
