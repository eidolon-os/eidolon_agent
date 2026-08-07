# Persona Runtime

The Agent has one persona domain: immutable semantic `PersonaGenome` snapshots
defined by `eidolon_sdk` and stored by `eidolon_data`.

## Runtime boundary

`PersonasService` is the public facade. Production wiring provides one
`RuntimeAuthorityPersonaGenomeStore`; there is no template registry, YAML instance,
behavioral-knob model, compiler DSL, or fallback persona.

The transport identity contains only Owner, Companion, and optional Device.
Agent resolves and validates `genome_id`, `genome_hash`, and Memory Realm from
the System Data Runtime Authority before every session. Existing sessions pin
that immutable snapshot after a later evolution commit.

## Realization

`PersonaRealizer` organizes the genome's semantic constitution, character,
relationship, and authored expression into model context. It consumes the
whole snapshot; it does not map numeric traits to hard-coded prompt fragments.
Unknown traits remain stored and do not need Agent support to survive a round
trip.

`ContextCompiler` owns Memory recall. Recalled `MemoryHit` records remain
evidence, are routed by `memory_realm_id`, and may add genome-owned relationship
guidance through `PersonaMemoryAdapter`. Memory never mutates a genome.

## Evolution

The domain contract and standalone profile retain the complete workflow below. Production
Agent currently has a read-only Runtime Authority adapter only; a separate, Owner-scoped and
idempotent System Data command contract is required before these mutations are enabled across
processes. Agent does not open System Data SQLite as a workaround.

Long-term changes use typed SDK events and complete candidate snapshots:

1. `persona.observation.created` records evidence.
2. `persona.evolution.proposed` stores a proposed immutable genome.
3. Approval writes `persona.evolution.approved`, commits the snapshot, and
   updates `companions.current_genome_id` in one transaction.
4. Rejection leaves the current pointer unchanged.
5. Rollback selects an earlier committed snapshot and writes an audit event.

Agent validation prevents memory-driven constitution rewrites, trait removal,
oversized deltas, and invalid relationship-stage jumps.

## Runtime state

Mood, energy, and attention are short-lived in-memory state. They may affect a
turn's volatile context but are never persisted into `genome_json`.

## Verification

The deterministic product-logic E2E is
`tests/e2e/test_persona_memory_e2e.py`. The reusable performance and regression
suite is `python -m eidolon_agent.app.benchmark.persona_memory`; its artifacts
are written under `benchmarks/runs/persona_memory/` for Admin discovery.
