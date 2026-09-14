from __future__ import annotations

from types import SimpleNamespace

import pytest
from eidolon_sdk.biz.persona import (
    PERSONA_GENOME_SCHEMA,
    PERSONA_REALIZER,
    build_default_persona_genome,
    persona_genome_hash,
    persona_genome_to_json,
)
from eidolon_sdk.biz.system_data import CompanionRuntimeSnapshot

from eidolon_agent.core.errors import DependencyError, NotFoundError
from eidolon_agent.infra.system_data import SystemDataCompanionRuntimeAuthority


def _snapshot(*, owner_id: str = "owner-1", genome_hash: str | None = None):
    genome = build_default_persona_genome(name="Annie")
    return CompanionRuntimeSnapshot.model_validate(
        {
            "contract_version": "1",
            "operation": "companion.runtime-snapshot",
            "owner_id": owner_id,
            "companion_id": "companion-1",
            "lifecycle_state": "active",
            "runtime_config": {"model": "local"},
            "memory_realm": {"realm_id": "realm-1", "lifecycle_state": "active"},
            "persona_genome": {
                "genome_id": "genome-1",
                "version": 2,
                "lifecycle_state": "committed",
                "schema_version": PERSONA_GENOME_SCHEMA,
                "genome_hash": genome_hash or persona_genome_hash(genome),
                "realizer_version": PERSONA_REALIZER,
                "genome": persona_genome_to_json(genome),
            },
        }
    )


class _Client:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    async def get_companion_runtime(self, companion_id, *, genome_id=None):
        self.calls.append((companion_id, genome_id))
        return self.snapshot


async def test_maps_wire_snapshot_to_owner_scoped_domain_facts() -> None:
    client = _Client(_snapshot())
    authority = SystemDataCompanionRuntimeAuthority(client)

    facts = await authority.resolve(
        owner_id="owner-1",
        companion_id="companion-1",
        genome_id="genome-1",
    )

    assert facts.memory_realm_id == "realm-1"
    assert facts.genome_id == "genome-1"
    assert facts.genome.constitution.name == "Annie"
    assert facts.runtime_config == {"model": "local"}
    assert client.calls == [("companion-1", "genome-1")]


async def test_fails_closed_on_owner_mismatch() -> None:
    with pytest.raises(NotFoundError):
        await SystemDataCompanionRuntimeAuthority(_Client(_snapshot(owner_id="owner-2"))).resolve(
            owner_id="owner-1",
            companion_id="companion-1",
        )


async def test_a_genome_hash_that_does_not_match_the_genome_is_carried_not_judged() -> None:
    """The hash is a label, and a label is not re-derived to be believed.

    Genome rows are append-only, so the id already names exactly one content;
    a digest beside it adds no fact the reader can act on. This used to be
    fatal, which meant any narrowing of the genome schema refused every
    Companion written under the wider one -- mid-conversation, with the person
    hearing nothing back. Shape compatibility is the reading contract's job.
    """

    facts = await SystemDataCompanionRuntimeAuthority(
        _Client(_snapshot(genome_hash="pg_wrong"))
    ).resolve(owner_id="owner-1", companion_id="companion-1")

    assert facts.genome_hash == "pg_wrong"
    assert facts.genome.constitution.name == "Annie"


async def test_maps_runtime_authority_precondition_to_domain_not_found() -> None:
    from eidolon_sdk.biz.system_data import SystemDataPrecondition

    async def _raise(*_args, **_kwargs):
        raise SystemDataPrecondition(412, "inactive")

    authority = SystemDataCompanionRuntimeAuthority(SimpleNamespace(get_companion_runtime=_raise))
    with pytest.raises(NotFoundError, match="inactive"):
        await authority.resolve(owner_id="owner-1", companion_id="companion-1")


async def test_maps_transport_failure_to_dependency_error() -> None:
    from eidolon_sdk.biz.system_data import SystemDataUnavailable

    async def _raise(*_args, **_kwargs):
        raise SystemDataUnavailable("connection refused")

    authority = SystemDataCompanionRuntimeAuthority(SimpleNamespace(get_companion_runtime=_raise))
    with pytest.raises(DependencyError, match="runtime authority unavailable"):
        await authority.resolve(owner_id="owner-1", companion_id="companion-1")
