# Personas

`eidolon_agent.personas` is the independent personality module. It owns persona
templates, per-user instance copies, prompt compilation, memory consumption and
automatic evolution.

## Boundary

Use `PersonasService` as the public facade. Application code should not reach
into the registry, store, compiler, memory adapter or evolution engine directly.

Templates are immutable base genomes. When a user binds a template, personas
creates a full `PersonaInstance` copy. Future evolution changes only that
instance copy.

## Runtime Data

Code and bundled templates live under:

```text
eidolon_agent/personas/
```

User instance data defaults to:

```text
personas/instances/<tenant>/<user>/<instance_id>.yaml
```

The runtime path is configurable via `settings.persona.instances_dir`.

## Public API

`PersonasService` exposes:

- `list_templates`
- `get_template`
- `create_instance`
- `get_instance`
- `compile_prompt`
- `evolve`
- `mock_memory_trigger`

`compile_prompt` reads memory through `PersonaMemoryPort`, adapts recalled facts
and graph relations through `PersonaMemoryAdapter`, then compiles the current
knobs into LLM-facing instructions.

## Ports

Personas depends on external infrastructure only through ports:

- `PersonaMemoryPort`
- `PersonaLLMPort`
- `PersonaEventPort`
- `PersonaAuditPort`

Production wires these ports to the existing agent memory, LLM, event bus and
audit stores. Tests use mocks.

## Automatic Evolution

Evolution is automatic by default, but guarded:

- `identity_core` is not evolvable.
- Knobs are clamped to their `min` and `max`.
- A rule cannot move a knob by more than its `step_limit`.
- Rule cooldowns prevent repeated rapid drift.
- Applied changes are returned as `PersonaEvolutionResult` and can be audited.

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
