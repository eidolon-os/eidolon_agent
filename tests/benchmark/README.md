# Replay Guardrails

Replay is split into three tiers:

- `in-process`: CI default. Uses in-memory collaborators and no real network.
- `live service`: hits a running agent admin HTTP API, gRPC Chat, SQLite turn trace, and the configured LLM/memory/NATS stack.
- `smoke`: the same live runner with real external dependencies enabled before release.

Useful commands:

```bash
./.venv/bin/python scripts/replay_experience.py \
  --output ~/eidolon/debug/reports/replay/in-process-latest.json \
  --markdown ~/eidolon/debug/reports/replay/in-process-latest.md

./.venv/bin/python scripts/bench_realtime.py \
  --mode live-service \
  --fixture tests/benchmark/fixtures/live_service_smoke.jsonl

./.venv/bin/python scripts/compare_replay_reports.py \
  ~/eidolon/debug/reports/replay/baseline.json \
  ~/eidolon/debug/reports/replay/candidate.json \
  --output ~/eidolon/debug/reports/replay/comparison.json
```

Live replay reports intentionally avoid persisted prompt text. They use gRPC
event kinds, assistant previews, admin observability summaries, and latency
metadata so reports stay useful without becoming prompt dumps.

## Live Local Contract Harness

Use the contract harness before live replay when you want to verify the local
stack boundary is actually reachable. It checks only public contracts: Agent
HTTP/admin endpoints, Admin gateway HTTP endpoints, Memory discovery, advertised
MCP tools, NATS turn publish, and optional Memory readback by source turn id.
It does not import `eidolon_memory` internals and does not start or own the dev
stack processes.

```bash
./.venv/bin/python -m eidolon_agent.app.benchmark.live_local_contract
```

With no services running, the command must fail with a JSON diagnostic report.
That is the expected readiness signal, not a flaky pass. For the optional live
Memory smoke, keep PR behavior deterministic and opt in explicitly:

```bash
EIDOLON_AGENT_LIVE_MEMORY_CONTRACT=1 \
  ./.venv/bin/python -m pytest -q tests/smoke/test_live_memory_contract.py -rs

EIDOLON_AGENT_LIVE_MEMORY_CONTRACT=1 \
EIDOLON_AGENT_LIVE_MEMORY_READBACK=1 \
  ./.venv/bin/python -m pytest -q tests/smoke/test_live_memory_contract.py -rs
```

The smoke profile reports unavailable live dependencies as `skipped`; the
harness report still marks those required checks as skipped so a real contract
run never pretends the stack passed.

## Live Benchmark User

Live benchmark scripts default to an isolated identity:

- tenant: `default`
- user: `benchmark`
- registry API: `http://127.0.0.1:9000/api` or `EIDOLON_BENCHMARK_REGISTRY_HTTP`

The scripts create or verify that user through `eidolon_admin` before pairing,
so benchmark runs do not write to a normal person's memory space. Use
`--user benchmark-voice` or another `benchmark-*` user for a dedicated profile.
Passing a non-benchmark user requires `--allow-non-benchmark-user`; that flag is
intended only for explicit debugging.

## Agent + Memory Experience Benchmark

The daily benchmark is `agent_memory_experience.v1`. It is a fast in-process
suite with 100+ deterministic multi-turn scenarios. It is designed for eidolon
agent experience rather than pure memory QA:

- current turn authority and topic switching
- long-term personalization recall
- memory update, abstention, privacy, temporary turns, and forgetting
- old tool failure / preamble drift prevention
- interruption recovery and multi-turn reference resolution
- latency and report stability

The taxonomy borrows capability ideas from LongMemEval, LoCoMo,
MemoryAgentBench, TAU-bench, and MultiChallenge, but the checks are local
eidolon product guardrails: fast, deterministic, no external LLM/network
dependency, and focused on whether agent + memory context is usable without
polluting the current turn.

Standard daily command:

```bash
./.venv/bin/python scripts/replay_experience.py \
  --agent-memory-benchmark \
  --output ~/eidolon/debug/reports/replay/agent-memory-latest.json \
  --markdown ~/eidolon/debug/reports/replay/agent-memory-latest.md \
  --html ~/eidolon/debug/reports/replay/agent-memory-latest.html
```

Real service E2E command:

```bash
./.venv/bin/python scripts/bench_realtime.py \
  --mode live-service \
  --agent-memory-benchmark \
  --http http://127.0.0.1:8081 \
  --grpc 127.0.0.1:45051 \
  --output-dir ~/eidolon/debug/reports/realtime
```

The live tier uses the running stack: real gRPC Chat, real admin HTTP, SQLite
turn traces, configured memory service, configured tool registry, and the
configured LLM provider. Its scenarios use unique benchmark tokens and trace
assertions so the report grades agent + memory behavior instead of free-form
wording style.

Acceptance gates for the fast benchmark:

- `report.passed == true`
- scenario pass rate: `1.0`
- check pass rate: `1.0`
- p95 first delta and p95 total latency do not regress against the latest
  accepted baseline
- no failed checks in `context_authority`, `agent_tool_control`, or
  `interrupt_realtime`

For service releases, run the same report comparison flow against a live replay
candidate:

```bash
./.venv/bin/python scripts/compare_replay_reports.py \
  ~/eidolon/debug/reports/replay/agent-memory-baseline.json \
  ~/eidolon/debug/reports/replay/agent-memory-latest.json \
  --output ~/eidolon/debug/reports/replay/agent-memory-comparison.json \
  --markdown ~/eidolon/debug/reports/replay/agent-memory-comparison.md
```
