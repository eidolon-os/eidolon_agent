from __future__ import annotations

import json

from eidolon_agent.app.benchmark.persona_memory import run_persona_memory_benchmark


async def test_persona_memory_benchmark_writes_admin_readable_run(tmp_path) -> None:
    report, artifacts = await run_persona_memory_benchmark(
        runs_dir=tmp_path,
        run_id="persona-memory-test",
        load_iterations=3,
    )

    assert report["passed"] is True
    assert report["summary"] == {"total": 4, "passed": 4, "failed": 0}
    manifest = json.loads((tmp_path / "persona_memory" / "persona-memory-test" / "manifest.json").read_text())
    assert manifest["passed"] is True
    assert manifest["run"]["suite"] == "persona_memory"
    assert artifacts["report"].endswith("report.json")
