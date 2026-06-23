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

./.venv/bin/python scripts/replay_live_service.py \
  --fixture tests/replay/fixtures/live_service_smoke.jsonl

./.venv/bin/python scripts/compare_replay_reports.py \
  ~/eidolon/debug/reports/replay/baseline.json \
  ~/eidolon/debug/reports/replay/candidate.json \
  --output ~/eidolon/debug/reports/replay/comparison.json
```

Live replay reports intentionally avoid persisted prompt text. They use gRPC
event kinds, assistant previews, admin observability summaries, and latency
metadata so reports stay useful without becoming prompt dumps.

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
./.venv/bin/python scripts/replay_live_service.py \
  --agent-memory-benchmark \
  --http http://127.0.0.1:8081 \
  --grpc 127.0.0.1:45051 \
  --output ~/eidolon/debug/reports/replay/live-agent-memory-latest.json \
  --markdown ~/eidolon/debug/reports/replay/live-agent-memory-latest.md \
  --html ~/eidolon/debug/reports/replay/live-agent-memory-latest.html
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
