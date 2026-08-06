# Agent OS Integration Boundaries

Status: accepted boundary; Channel Provider integration is blocked on a stable contract.

## Owned here

- Owner-scoped Companion runtime and turn execution.
- Persona/genome consumption, model/tool orchestration, and source attribution.
- Agent-local async workflows and the existing Agent-to-Memory write protocol.

`device_id` may be present in `TurnContext` or on a persisted turn, but it is contextual input.
It does not make Agent a Device authority and it is not required for a virtual
Companion session.

`owner_id` is the only security/namespace principal. `input_modality`, `device_id`,
`session_id`, `request_id`, and `trace_id` describe an input and its execution; none is a
second user identity. A producer component is recorded as `source` on audit facts, not as
an `actor` principal. A delegated-principal model is intentionally absent until Eidolon OS
has a real delegation feature with explicit authorization semantics.

## Owned elsewhere

- Hub: Device enrollment, registry, manifest, approval/revocation, and owner admission.
- Kernel: global Owner namespace, Device mount, optional Companion attachment, CAS,
  authoritative state, and audit.
- Channel: audio/data/media providers, live connection state, capability directory, and
  Device commands.
- Memory: memory engine protocols and storage internals.

NATS is retained only where a concrete Agent/Memory or Agent-local asynchronous protocol
already exists. It is not a universal OS IPC, shared blackboard, or source of Device
authority.

## Removed legacy path

Hub's current code has no runtime Device command endpoints, command persistence, presence
runtime, or `EIDOLON_RUNTIME_DEVICES` KV owner. Agent's adapters for those removed APIs were
therefore invalid and have been deleted. The pure domain ports and service remain because
they express the capability the brain needs without selecting a transport or authority.

## Blocker and next gate

Do not invent a replacement endpoint in Agent. Re-enable `body_control` only after Channel
publishes and tests a narrow versioned contract for:

1. Owner-scoped provider/capability listing with live availability.
2. Owner-scoped command submission with idempotency and a terminal receipt.
3. Exact authorization semantics tied to the selected Owner/Companion runtime context.

The future adapter belongs in Agent's infrastructure layer and must implement the existing
domain ports. It must not leak optional Device/Companion attachment decisions into the
audio or turn pipeline.
