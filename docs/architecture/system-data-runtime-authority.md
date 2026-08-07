# System Data Runtime Authority

Status: runtime read path implemented; Persona mutation contract intentionally blocked.

## Decision

Production Agent does not import the System Data composition root, ORM rows, or SQLite
path. It consumes the versioned Companion Runtime Snapshot HTTP contract through the
domain-facing `CompanionRuntimeAuthority` Port.

Channel's V5 runtime token authenticates only the selected Owner/Companion boundary, a
required Session, and an optional Device source. When a gRPC call enters the application
boundary, Agent resolves the Companion from System Data and validates:

- token Owner equals snapshot Owner;
- Companion and Memory Realm are active;
- Persona Genome is committed;
- schema and realizer versions are supported;
- the normalized Genome content matches its declared hash.
- known Companion runtime policy fields have valid types.

Transport failures map to `UNAVAILABLE`; missing, inactive, cross-Owner, or invalid runtime
facts fail closed before an Agent instance or TurnContext is created.

The resolved facts and typed runtime policy form one immutable
`AuthorizedRuntimeSession`. A long-lived Chat stream reuses that snapshot for every Turn;
there is no per-Turn HTTP lookup and no permissive fallback when Data is unavailable.
Authority changes take effect on the next authenticated session/stream. See
`runtime-session-boundary.md` for the transport and namespace invariants.

## Composition

- Production: `SystemDataCompanionRuntimeAuthority` over
  `SystemDataRuntimeClient` and the `/api/companion-authority/v1` HTTP contract.
- Standalone development/test: `LocalCompanionRuntimeAuthority` over current Data V2
  commands/repositories. The container field is explicitly named `local_system_data`; this
  path is not a production fallback.
- Agent's own conversations, turns, messages, jobs, runtime sessions, and audit outbox stay
  in `eidolon-agent.sqlite3`.

The current service bearer is a deployment credential required by the existing local Data
authority endpoint. It is not an Owner token and is never accepted as an Agent caller
identity. Replacing it depends on a system-wide local service identity decision; Agent does
not invent that identity root.

## Deliberate blocker

The stable HTTP contract currently exposes runtime reads and Companion face reads only.
Production Persona observation/evolution commands therefore remain unavailable through
this adapter. Agent must not regain that behavior by opening Data SQLite or inventing a
private write endpoint. The next contract review should define the smallest Owner-scoped,
idempotent Persona command surface in System Data, then inject it as a separate command
Port. This is not a regression from a valid production writer: before this cutover,
non-standalone Agent explicitly opened Data with `sqlite_read_only=True`, so attempts to
delegate these commands through that connection could not commit legally. Standalone tests
retain the existing Data V2 command behavior meanwhile.
