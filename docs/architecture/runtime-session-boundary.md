# Runtime Session Boundary

Status: accepted and implemented for Agent V5 gRPC entrypoints.

## Decision

Every Agent RPC is authorized through one `AuthorizedRuntimeSession`. It is built from two
inputs only:

1. the verified V5 token: `owner_id`, `companion_id`, required `session_id`, and optional
   `device_id`;
2. the Owner-scoped Companion Runtime Authority snapshot from System Data.

The application service verifies that the authority returned the same Owner and Companion,
maps its known runtime policy fields to a typed immutable config, and rejects missing session
binding or malformed policy. The transport then passes this scope inward; Turn code does not
call System Data and does not reconstruct authorization from request metadata.

## Namespace invariants

- `owner_id` is the sole security/namespace principal.
- `companion_id` is the selected runtime resource inside that Owner namespace.
- `session_id` is the signed live connection boundary, not a user identity.
- `device_id` is an optional source fact, not a principal and not required for a virtual
  Companion.
- `conversation_id` is a durable history/context key inside the already-authorized
  Owner/Companion scope. It is intentionally distinct from `session_id` so a new LiveKit room
  can continue an existing conversation without inheriting another live session's signals.

Consequently, `PushSignal` has no request-level session selector and
`SubscribeProactive` has no request-level Companion/instance selector. Signal buffers are
keyed by the authenticated session. Proactive subscriptions are keyed by the authenticated
Companion. Cross-Owner or cross-session selection is therefore absent from the wire contract,
not repaired later by scattered ACL checks.

## Snapshot lifetime

A Chat stream resolves one authority snapshot and pins its Genome, Realm, versions, and typed
runtime config for all Turns on that stream. This gives one coherent Companion execution
environment and keeps transient authority calls out of the Turn hot path.

If token verification, System Data resolution, Owner matching, session binding, or config
validation fails, the RPC fails closed before constructing a Turn. There is no cached default
or permissive fallback. Changes to Companion policy apply to the next authenticated stream;
an orchestrated reconnect/revocation is required when a control-plane change must take effect
immediately.

## Contract verification

The integration contract test composes a real System Data SQLite store and versioned ASGI HTTP
app with the SDK V5 signer/verifier and Agent's production HTTP authority adapter. It proves
that a session-bound token resolves to one immutable Owner/Companion scope and that the same
Companion cannot be authorized through another Owner token.

Agent intentionally does not import Channel for this test. Channel and Agent meet at the shared
SDK token contract, while Agent and Data meet at the versioned Runtime Snapshot HTTP contract.
This keeps the dependency direction identical to production instead of creating a test-only
reverse dependency.

## Deployment assumption

Channel signs the Agent token after resolving Kernel Mount and System Data facts. The token's
`session_id` is the current LiveKit room name and is cached only for that room-bound Channel
job. Admin test and benchmark callers mint their own unique session ids. The existing shared
HS256 secret remains a deployment mechanism, not an Owner account token or a Kernel identity
root; replacing it is outside this boundary and must not change the domain Port.
