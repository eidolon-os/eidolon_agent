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
