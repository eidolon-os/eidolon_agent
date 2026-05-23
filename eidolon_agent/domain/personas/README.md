# Personas

`eidolon_agent.domain.personas` is the persona subsystem: templates, per-user
instance copies, runtime state, prompt compilation, memory consumption,
signal interpretation, and asynchronous evolution.

## Boundary

Use `PersonasService` as the public facade. Application code should not reach
into the registry, instance store, compiler, runtime state, memory adapter, or
evolution worker directly.

Templates are immutable base genomes. When a user binds a template, personas
creates a full `PersonaInstance` copy. Future evolution changes only that
instance copy. Short-lived mood, energy, and attention live in runtime state
(in memory + periodic snapshot) and are not written into instance YAML.

## Runtime Data

Code and bundled templates live under:

```text
eidolon_agent/domain/personas/
├── templates/        # bundled genome YAMLs (read-only)
└── …                 # service.py, registry.py, instance_store.py, …
```

User instance data defaults to:

```text
~/eidolon/personas/instances/<tenant>/<user>/<instance_id>.yaml
```

The runtime path is configurable via `settings.persona.instances_dir`.

## Public API — what callers actually use

The hot path (TurnEngine, gRPC servicer) only needs three methods:

- `compile_prompt` — assemble the persona prompt for a Turn
- `submit_signal` — push a realtime signal digest into the runtime state
- `submit_interaction` — queue a turn interaction event for the async
  evolution worker

The remaining methods are admin / inspection surfaces consumed by the admin
HTTP routers and tests:

- `list_templates`, `get_template`
- `create_instance`, `get_instance`, `get_snapshot`
- `update_runtime_state`
- `evolve`, `evolve_now`, `drain_evolution_queue`
- `mock_memory_trigger`

(`propose_proactive` was removed in the simplification refactor; the proactive
engine itself is gone — proactive triggers will come back via a NATS-driven
worker when product needs them.)

`compile_prompt` reads memory through `PersonaMemoryPort`, adapts recalled
facts and graph relations through `PersonaMemoryAdapter`, then compiles the
current knobs and runtime state into LLM-facing instructions. Note: when
called from the `ContextCompiler` on the hot path, callers pass
`dry_run_memory=[]` to skip the inner memory recall — memory is owned by the
compiler, not by personas, to avoid double-fetch.

## Ports

Personas depends on external infrastructure only through ports declared in
`personas/ports.py`:

- `PersonaMemoryPort` — memory recall (typically the same `EidolonMemoryPort`
  instance from `infra/memory/`)
- `PersonaLLMPort` — optional, for evolution-time summarisation
- `PersonaEventPort` — publish `persona.overlay.updated` / `evolution.applied`
  events
- `PersonaAuditPort` — append-only audit of applied evolution rules
- `PersonaEvolutionRepository` — SQLite history (implemented by
  `infra/persistence/SqlEvolutionHistoryRepository`)

Production wires these ports through `app/runtime/bootstrap.py`. Tests use
mocks or null ports.

## Automatic Evolution

Interaction-driven evolution is asynchronous, guarded, and isolated from the
turn pipeline:

- A separate worker (`PersonaEvolutionWorker`) consumes `PersonaInteractionEvent`
  via an internal queue.
- `identity_core` knobs are not evolvable.
- Knobs are clamped to their `min` and `max`.
- A rule cannot move a knob by more than its `step_limit`.
- Rule cooldowns prevent repeated rapid drift.
- Applied changes are returned as `PersonaEvolutionResult`, audited, and
  persisted via the repository port.
- Runtime state changes are fast in-memory updates; long-term knob changes
  go through the worker so the turn pipeline never blocks on persistence.

## Runtime State

Personas owns short-lived mood, energy, and attention. Other modules submit
turn interactions or realtime signal digests; they do not mutate state
directly. There is no LLM-callable mood / persona-state mutation tool.

## Mock Memory Trigger

`mock_memory_trigger` lets the admin endpoint preview how the persona's
memory policies react to synthetic `MemoryHit` records. It can run as dry-run
or actually apply triggered evolution rules.

## Tests

Module-local tests live alongside the code:

```bash
.venv/bin/pytest eidolon_agent/domain/personas/tests/   # personas only
.venv/bin/pytest -m functional                           # all functional tests
.venv/bin/pytest                                         # full suite (222 tests)
```
