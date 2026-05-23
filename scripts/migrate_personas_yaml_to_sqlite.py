"""One-shot migration: YAML persona-instance files → SQLite.

Reads every ``*.yaml`` under ``~/eidolon/personas/instances/<tenant>/<user>/``,
parses each into a :class:`PersonaInstance`, and ``INSERT``s a row into the
``persona_instances`` table. Existing rows are left untouched (the import is
strictly additive).

Usage::

    .venv/bin/python -m scripts.migrate_personas_yaml_to_sqlite

The script reads ``config/config.yaml`` (or ``$EIDOLON_AGENT_SETTINGS_YAML``)
for paths so the same settings tree that drives the runtime drives the
migration. YAML files are *not* deleted afterwards — the operator should
verify the SQLite contents and then clean up by hand.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from eidolon_agent.config.settings import load_settings
from eidolon_agent.domain.personas import YamlPersonaInstanceStore
from eidolon_agent.infra.persistence import (
    SqlPersonaInstanceStore,
    create_engine,
    create_session_factory,
    ensure_schema,
)
from eidolon_agent.infra.persistence.repositories import SqlPersonaInstanceRepository

logging.basicConfig(level=logging.INFO, format="%(message)s")
_log = logging.getLogger("personas-migrate")


async def migrate() -> int:
    settings = load_settings()
    yaml_dir = Path(settings.persona.instances_dir)
    if not yaml_dir.exists():
        _log.info("nothing to migrate: %s does not exist", yaml_dir)
        return 0

    engine = create_engine(settings.sqlite)
    await ensure_schema(engine)
    session_factory = create_session_factory(engine)

    yaml_store = YamlPersonaInstanceStore(yaml_dir)
    sql_store = SqlPersonaInstanceStore(session_factory)

    instances = await yaml_store.list_all()
    _log.info("found %d YAML instances under %s", len(instances), yaml_dir)

    migrated = 0
    skipped = 0
    for inst in instances:
        async with session_factory() as session:
            repo = SqlPersonaInstanceRepository(session)
            existing = await repo.get(inst.tenant_id, inst.user_id, inst.instance_id)
        if existing is not None:
            _log.info(
                "skip %s/%s/%s (already in SQLite)",
                inst.tenant_id,
                inst.user_id,
                inst.instance_id,
            )
            skipped += 1
            continue
        # ``reason='migration'`` tells SqlPersonaInstanceStore.save not to bump
        # last_active_at — these are historical writes, not user interactions.
        await sql_store.save(inst, reason="migration")
        _log.info(
            "migrated %s/%s/%s (overlay_version=%d)",
            inst.tenant_id,
            inst.user_id,
            inst.instance_id,
            inst.overlay_version,
        )
        migrated += 1

    await engine.dispose()
    _log.info("done: migrated=%d skipped=%d", migrated, skipped)
    _log.info(
        "YAML files left in place. To clean up after verifying: rm -rf %s",
        yaml_dir,
    )
    return 0


def main() -> int:
    return asyncio.run(migrate())


if __name__ == "__main__":
    sys.exit(main())
