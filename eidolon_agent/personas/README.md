# Personas

`eidolon_agent.personas` is the independent personality module. It owns persona
templates, per-user instance copies, runtime state, prompt compilation, memory
consumption, signal interpretation, proactive policy and automatic evolution.

## Boundary

Use `PersonasService` as the public facade. Application code should not reach
into the registry, store, compiler, runtime state, memory adapter or evolution
engine directly.

Templates are immutable base genomes. When a user binds a template, personas
creates a full `PersonaInstance` copy. Future evolution changes only that
instance copy. Short-lived mood, energy and attention live in runtime state and
are not written into instance YAML.

## Runtime Data

Code and bundled templates live under:

```text
eidolon_agent/personas/
```

User instance data defaults to:

```text
~/eidolon/personas/instances/<tenant>/<user>/<instance_id>.yaml
```

The runtime path is configurable via `settings.persona.instances_dir`.

## Public API

`PersonasService` exposes:

- `list_templates`
- `get_template`
- `create_instance`
- `get_instance`
- `get_snapshot`
- `compile_prompt`
- `submit_interaction`
- `submit_signal`
- `evolve_now`
- `mock_memory_trigger`
- `propose_proactive`

`compile_prompt` reads memory through `PersonaMemoryPort`, adapts recalled facts
and graph relations through `PersonaMemoryAdapter`, then compiles the current
knobs and runtime state into LLM-facing instructions.

## Ports

Personas depends on external infrastructure only through ports:

- `PersonaMemoryPort`
- `PersonaLLMPort`
- `PersonaEventPort`
- `PersonaAuditPort`

Production wires these ports to the existing agent memory, LLM, event bus and
audit stores. Tests use mocks.

## Automatic Evolution

Interaction-driven evolution is asynchronous by default, but guarded:

- `identity_core` is not evolvable.
- Knobs are clamped to their `min` and `max`.
- A rule cannot move a knob by more than its `step_limit`.
- Rule cooldowns prevent repeated rapid drift.
- Applied changes are returned as `PersonaEvolutionResult` and can be audited.
- Runtime state changes are fast in-memory updates; long-term knob changes are
  persisted by the evolution worker.

## Runtime State

Personas owns short-lived mood, energy and attention. Other modules submit
turns or realtime signal digests to personas; they do not mutate state directly.
There is no LLM-callable mood/persona-state mutation tool.

## Mock Memory Trigger

`mock_memory_trigger` lets tests and Admin preview how persona memory policies
react to synthetic `MemoryHit` records. It can run as dry-run or apply triggered
evolution rules.

## Tests

Run the module tests:

```bash
pytest tests/personas
```

Run the integration slice:

```bash
pytest tests/personas tests/context tests/agent
```
