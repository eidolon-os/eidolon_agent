from __future__ import annotations

import asyncio

import pytest

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.domain.agent.registry import AgentRegistry

pytestmark = pytest.mark.unit


async def test_registry_resolves_dynamic_data_genome() -> None:
    created = []

    async def _factory(instance):
        created.append(instance)
        return object()

    registry = AgentRegistry(instance_factory=_factory)

    first = await registry.resolve_runtime(
        owner_id="benchmark",
        companion_id="test",
        genome_id="genome-benchmark",
    )
    second = await registry.resolve_runtime(
        owner_id="benchmark",
        companion_id="test",
        genome_id="genome-benchmark",
    )

    assert first is second
    assert first.companion_id == "test"
    assert first.genome_id == "genome-benchmark"
    assert len(created) == 1


async def test_registry_rejects_identity_without_pinned_genome() -> None:
    async def _factory(instance):
        return object()

    registry = AgentRegistry(instance_factory=_factory)
    with pytest.raises(NotFoundError, match="does not pin"):
        await registry.resolve_runtime(
            owner_id="owner",
            companion_id="companion",
        )


async def test_two_companions_of_one_owner_never_share_a_runtime() -> None:
    """The "Persona 不串线" property, at the place it would go wrong.

    One Owner now has several Companions and they run at the same time. The key
    is owner + companion + genome, so the isolation is structural rather than a
    rule someone applies — but a key is easy to "simplify" to owner alone by
    someone who has only ever seen one Companion per Owner, and the two runtimes
    would then be one.
    """

    created = []

    async def _factory(instance):
        created.append((instance.owner_id, instance.companion_id, instance.genome_id))
        return object()

    registry = AgentRegistry(instance_factory=_factory)

    mochi = await registry.resolve_runtime(
        owner_id="owner-1", companion_id="c-mochi", genome_id="g-mochi"
    )
    nori = await registry.resolve_runtime(
        owner_id="owner-1", companion_id="c-nori", genome_id="g-nori"
    )

    assert mochi is not nori
    assert mochi.agent is not nori.agent
    assert len(created) == 2
    assert {entry[1] for entry in created} == {"c-mochi", "c-nori"}


async def test_the_same_companion_on_a_new_genome_is_a_new_runtime() -> None:
    """Restoring a persona has to take effect.

    The genome is in the key because a Companion whose persona was rolled back
    is not the same runtime — reusing the instance would keep answering as the
    version the Owner just replaced.
    """

    async def _factory(instance):
        return object()

    registry = AgentRegistry(instance_factory=_factory)

    before = await registry.resolve_runtime(
        owner_id="owner-1", companion_id="c-mochi", genome_id="g-1"
    )
    after = await registry.resolve_runtime(
        owner_id="owner-1", companion_id="c-mochi", genome_id="g-2"
    )

    assert before is not after


async def test_concurrent_first_calls_build_one_runtime_each() -> None:
    """Two Companions resolving at once, which is the ordinary case now.

    The lock exists so a single Companion is not built twice; what this checks
    is that it does not accidentally serialise two *different* Companions into
    one instance — the factory must run once per key, not once per lock.
    """

    started = 0

    async def _factory(instance):
        nonlocal started
        started += 1
        # Yield inside the factory, so both callers are inside resolve_runtime
        # at the same time rather than one after the other.
        await asyncio.sleep(0)
        return object()

    registry = AgentRegistry(instance_factory=_factory)

    mochi, mochi_again, nori = await asyncio.gather(
        registry.resolve_runtime(
            owner_id="owner-1", companion_id="c-mochi", genome_id="g-mochi"
        ),
        registry.resolve_runtime(
            owner_id="owner-1", companion_id="c-mochi", genome_id="g-mochi"
        ),
        registry.resolve_runtime(
            owner_id="owner-1", companion_id="c-nori", genome_id="g-nori"
        ),
    )

    assert mochi is mochi_again, "one Companion, one runtime"
    assert mochi is not nori
    assert started == 2, "one build per Companion, not per call and not per Owner"
    assert len(registry.list_instances()) == 2


async def test_owners_do_not_collide_through_a_shared_companion_id() -> None:
    """Companion ids are unique in practice; the key does not rely on it.

    If it did, two Owners who happened to name a Companion the same way would
    share a runtime — the worst possible crosstalk, across people rather than
    within one.
    """

    async def _factory(instance):
        return object()

    registry = AgentRegistry(instance_factory=_factory)

    mine = await registry.resolve_runtime(
        owner_id="owner-1", companion_id="c-same", genome_id="g-1"
    )
    theirs = await registry.resolve_runtime(
        owner_id="owner-2", companion_id="c-same", genome_id="g-1"
    )

    assert mine is not theirs


async def test_a_hosts_runtimes_are_per_owner_not_per_host() -> None:
    """The filter that keeps one person's Eidolons out of another's list.

    The registry holds every Owner on this Host. Filtering lives in it rather
    than at each caller because a caller writing its own comprehension over
    ``list_instances`` is one typo away from showing somebody else's Companions
    — and that mistake would look right in every test that only ever set up one
    Owner.
    """

    async def _factory(instance):
        return object()

    registry = AgentRegistry(instance_factory=_factory)
    for owner, companion in [
        ("owner-1", "c-a"),
        ("owner-1", "c-b"),
        ("owner-2", "c-c"),
    ]:
        await registry.resolve_runtime(
            owner_id=owner, companion_id=companion, genome_id=f"g-{companion}"
        )

    assert {inst.companion_id for inst in registry.for_owner("owner-1")} == {
        "c-a",
        "c-b",
    }
    assert [inst.companion_id for inst in registry.for_owner("owner-2")] == ["c-c"]
    assert registry.for_owner("owner-3") == []


async def test_several_companions_are_live_at_once() -> None:
    """The case the product was pretending did not exist.

    A Host keeps runtime context per Companion (plan §4.6), so more than one
    being live is ordinary rather than exotic. Screens were deriving "which one
    is running" from whether the Owner had a default — a routing fallback — and
    could therefore only ever show one.
    """

    async def _factory(instance):
        return object()

    registry = AgentRegistry(instance_factory=_factory)
    await registry.resolve_runtime(
        owner_id="owner-1", companion_id="c-a", genome_id="g-a"
    )
    await registry.resolve_runtime(
        owner_id="owner-1", companion_id="c-b", genome_id="g-b"
    )

    assert len(registry.for_owner("owner-1")) == 2


async def test_being_addressed_is_what_makes_a_runtime_recent() -> None:
    """Newest use first, and use is what moves it.

    ``last_active_at`` was a field nobody wrote for as long as nobody read it,
    so a list ordered by it would have been ordered by nothing. It is touched
    inside ``resolve_runtime`` rather than at the call sites, so a new way of
    addressing a Companion cannot forget to keep it true.
    """

    async def _factory(instance):
        return object()

    registry = AgentRegistry(instance_factory=_factory)
    first = await registry.resolve_runtime(
        owner_id="owner-1", companion_id="c-a", genome_id="g-a"
    )
    second = await registry.resolve_runtime(
        owner_id="owner-1", companion_id="c-b", genome_id="g-b"
    )
    assert [inst.companion_id for inst in registry.for_owner("owner-1")] == [
        "c-b",
        "c-a",
    ]

    # Addressing the older one again moves it to the front, because it is now
    # the one in use.
    await registry.resolve_runtime(
        owner_id="owner-1", companion_id="c-a", genome_id="g-a"
    )
    assert [inst.companion_id for inst in registry.for_owner("owner-1")] == [
        "c-a",
        "c-b",
    ]
    assert first.last_active_at is not None
    assert second.last_active_at is not None
