# Agent Runtime Persistence Cutover

- Status: implementation prepared; production activation pending
- Legacy data: no migration or compatibility reader
- Target file: `~/eidolon/data/eidolon-agent.sqlite3`

## Authority boundary

Agent owns only runtime facts it creates:

- runtime sessions;
- conversations, turns, and messages;
- long-running jobs and their progress/result state;
- the Agent-local global-audit outbox.

Owner, Companion, Persona, Realm, Device admission, Kernel attachment, Memory
facts, and the global audit query index are intentionally absent. Their IDs are
opaque references copied from a verified runtime context. There are no
cross-database foreign keys and no Data lookup in the terminal-turn write.

The implementation is in `infra/persistence/runtime_store.py` and
`infra/persistence/agent_runtime.py`. Production wiring still uses the existing
DataStore session factory until the release gate below is complete; the runtime
implementation itself imports no Data model.

## Frequency policy

- One completed turn is one SQLite transaction containing the session touch,
  conversation, turn, and final user/assistant messages.
- Tokens, deltas, phases, fanout observations, polling attempts, and progress
  samples do not create global audit rows.
- Job progress remains in the Agent store. Only terminal asynchronous outcomes
  (`succeeded`, `failed`, `cancelled`, `timed_out`) create a receipt in the
  local outbox transaction.
- Audit publishing is asynchronous, batched, exponentially backed off, and
  uses a separate JetStream connection. It never runs in a request coroutine.

## SQLite profile

The authority uses WAL, `synchronous=FULL`, foreign keys, an explicit busy
timeout/checkpoint policy, and a single pooled writer connection. Schema V1 is
a clean baseline identified by `PRAGMA user_version=1`; no table or row is
copied from `eidolon.sqlite3`.

## Atomic activation gate

Before production bootstrap may point at the new file, all of these must land
in the same verified release:

1. Agent Admin conversation and job readers use the runtime authority with no
   Data fallback.
2. Admin owner deletion journals and retries Agent-runtime deletion separately
   from system-data and Memory deletion.
3. Admin Mission Control composes Agent runtime, operational telemetry, and the
   global audit index instead of querying Data runtime/Event tables.
4. Replay, benchmark, acceptance, and backup tooling use the Agent query port.
5. The Agent audit dispatcher starts and stops with Agent lifecycle, while NATS
   failure leaves request handling available.
6. Cross-repository acceptance proves new turns appear only in
   `eidolon-agent.sqlite3` and no runtime write reaches `eidolon-system.sqlite3`.

After that release passes, activation creates an empty Agent runtime database.
The legacy runtime tables are dropped from Data in the subsequent clean V2
baseline; they are not read, copied, or retained for compatibility.
