"""Deterministic benchmark for the Agent persona, memory, and evolution chain."""

from __future__ import annotations

import argparse
import asyncio
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from eidolon_data import DataSettings, DataStore
from eidolon_sdk.biz.persona import (
    PersonaEvidenceRef,
    PersonaEvolutionProposalEvent,
    PersonaMemoryPolicy,
    PersonaObservationEvent,
    build_default_persona_genome,
    persona_genome_to_json,
)

from eidolon_agent.core.types.memory import MemoryHit, MemoryKind
from eidolon_agent.domain.personas import PersonasService
from eidolon_agent.infra.benchmark import write_standard_benchmark_run
from eidolon_agent.infra.persistence import RuntimeAuthorityPersonaGenomeStore
from eidolon_agent.infra.system_data import LocalCompanionRuntimeAuthority

SUITE = "persona_memory"


async def run_persona_memory_benchmark(
    *,
    runs_dir: Path,
    run_id: str | None = None,
    load_iterations: int = 30,
) -> tuple[dict, dict[str, str]]:
    run_id = run_id or datetime.now(timezone.utc).strftime("persona-memory-%Y%m%dT%H%M%SZ")
    cases: list[dict] = []
    metrics: dict[str, dict] = {}

    with tempfile.TemporaryDirectory(prefix="eidolon-persona-benchmark-") as tmp:
        store = DataStore.open(DataSettings(sqlite_path=str(Path(tmp) / "benchmark.sqlite3")))
        await store.init_schema()
        try:
            provision_started = time.perf_counter()
            await store.owner_commands.create_owner(
                owner_id="benchmark-owner",
                display_name="Owner",
            )
            genome = build_default_persona_genome(
                name="Benchmark Companion", origin="owner_authored"
            )
            genome = genome.model_copy(
                update={
                    "memory_policy": PersonaMemoryPolicy(
                        recall_policy={"use_memory_as_evidence": True},
                        relation_policies={
                            "owner.preference.response_style": {
                                "guidance": "保持简洁，并明确记忆依据。"
                            }
                        },
                    )
                }
            )
            workspace = await store.companion_workspaces.provision_workspace(
                owner_id="benchmark-owner",
                companion_id="benchmark-companion",
                companion_display_name="Benchmark Companion",
                genome_id="benchmark-genome-origin",
                genome_json=persona_genome_to_json(genome),
                realm_id="benchmark-realm",
            )
            provision_ms = _elapsed_ms(provision_started)
            observations: list[PersonaObservationEvent] = []

            async def _record_observation(event: PersonaObservationEvent) -> None:
                observations.append(event)

            service = PersonasService(
                store=RuntimeAuthorityPersonaGenomeStore(
                    LocalCompanionRuntimeAuthority(store),
                    evolution_commands=store.persona_commands,
                    observation_sink=_record_observation,
                )
            )
            cases.append(
                _case(
                    "workspace_integrity",
                    provision_ms,
                    workspace.companion.current_genome_id == workspace.persona_genome.genome_id
                    and workspace.memory_realm.companion_id == workspace.companion.companion_id,
                    "Owner, companion, genome, and memory realm are atomically provisioned.",
                )
            )

            load_samples: list[float] = []
            snapshots = []
            for _ in range(load_iterations):
                started = time.perf_counter()
                snapshots.append(
                    await service.get_snapshot(
                        owner_id="benchmark-owner",
                        companion_id="benchmark-companion",
                    )
                )
                load_samples.append(_elapsed_ms(started))
            load_stats = _latency_stats(load_samples)
            metrics["genome_load_ms"] = load_stats
            cases.append(
                _case(
                    "pinned_genome_load",
                    sum(load_samples),
                    len({item.stored.genome_hash for item in snapshots}) == 1,
                    "Repeated hot-path reads resolve one immutable id/hash.",
                    metrics=load_stats,
                )
            )

            hit = MemoryHit(
                id="benchmark-memory-evidence",
                content="Owner prefers concise answers.",
                kind=MemoryKind.PREFERENCE,
                similarity=0.95,
                metadata={"relation_type": "owner.preference.response_style"},
            )
            realize_started = time.perf_counter()
            realized = await service.realize_context(
                owner_id="benchmark-owner",
                companion_id="benchmark-companion",
                user_text="How should you answer?",
                dry_run_memory=[hit],
            )
            realize_ms = _elapsed_ms(realize_started)
            metrics["persona_realize_ms"] = {"value": realize_ms}
            cases.append(
                _case(
                    "memory_evidence_realization",
                    realize_ms,
                    hit.content in realized.memory_block
                    and "保持简洁" in realized.memory_block
                    and realized.evidence_refs[0].ref_id == hit.id,
                    "Memory is evidence consumed by genome policy, not a genome mutation.",
                )
            )

            base = snapshots[-1].stored
            evidence = PersonaEvidenceRef(
                kind="memory_fragment",
                ref_id=hit.id,
                summary=hit.content,
                confidence=0.95,
            )
            await service.record_observation(
                PersonaObservationEvent(
                    observation_id="benchmark-observation",
                    owner_id=base.owner_id,
                    companion_id=base.companion_id,
                    kind="response_style_preference",
                    source="memory_reflection",
                    summary=hit.content,
                    confidence=0.95,
                    evidence_refs=[evidence],
                )
            )
            trait = base.genome.character.traits["core.structure"]
            traits = dict(base.genome.character.traits)
            traits["core.structure"] = trait.model_copy(
                update={"value": trait.value + 0.04, "source": "memory_reflection"}
            )
            candidate = base.genome.model_copy(
                update={
                    "character": base.genome.character.model_copy(update={"traits": traits}),
                    "provenance": base.genome.provenance.model_copy(
                        update={
                            "origin": "memory_reflection",
                            "base_genome_id": base.genome_id,
                            "evidence_refs": [evidence],
                        }
                    ),
                }
            )
            evolution_started = time.perf_counter()
            proposed = await service.create_evolution_proposal(
                PersonaEvolutionProposalEvent(
                    proposal_id="benchmark-proposal",
                    owner_id=base.owner_id,
                    companion_id=base.companion_id,
                    base_genome_id=base.genome_id,
                    base_genome_hash=base.genome_hash,
                    proposed_genome_id="benchmark-genome-evolved",
                    confidence=0.95,
                    rationale="Repeated evidence supports a bounded adjustment.",
                    proposed_genome=candidate,
                    evidence_refs=[evidence],
                )
            )
            committed = await service.approve_evolution(
                owner_id=base.owner_id,
                companion_id=base.companion_id,
                proposed_genome_id=proposed.genome_id,
                expected_base_genome_id=base.genome_id,
            )
            pinned = await service.get_snapshot(
                owner_id=base.owner_id,
                companion_id=base.companion_id,
                genome_id=base.genome_id,
                genome_hash=base.genome_hash,
            )
            await service.rollback(
                owner_id=base.owner_id,
                companion_id=base.companion_id,
                genome_id=base.genome_id,
            )
            evolution_ms = _elapsed_ms(evolution_started)
            metrics["evolution_transaction_ms"] = {"value": evolution_ms}
            cases.append(
                _case(
                    "governed_evolution",
                    evolution_ms,
                    committed.genome_hash != base.genome_hash
                    and pinned.stored.genome_hash == base.genome_hash,
                    "Evolution commits a new snapshot while an old session stays pinned.",
                )
            )
        finally:
            await store.close()

    failed = sum(1 for case in cases if not case["passed"])
    report = {
        "schema_version": "eidolon_agent.persona_memory_benchmark",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "kind": "persona_memory_benchmark",
        "mode": "local_sqlite",
        "profile": "deterministic",
        "passed": failed == 0,
        "summary": {
            "total": len(cases),
            "passed": len(cases) - failed,
            "failed": failed,
        },
        "metrics": metrics,
        "scenarios": cases,
    }
    artifacts = write_standard_benchmark_run(report, runs_dir=runs_dir, suite=SUITE)
    return report, artifacts


def _case(
    case_id: str,
    elapsed_ms: float,
    passed: bool,
    detail: str,
    *,
    metrics: dict | None = None,
) -> dict:
    return {
        "case_id": case_id,
        "passed": passed,
        "elapsed_ms": round(elapsed_ms, 3),
        "detail": detail,
        "metrics": metrics or {},
    }


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _latency_stats(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
        "max": round(max(ordered), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path(__file__).resolve().parents[3] / "benchmarks" / "runs",
    )
    parser.add_argument("--run-id")
    parser.add_argument("--load-iterations", type=int, default=30)
    args = parser.parse_args()
    report, artifacts = asyncio.run(
        run_persona_memory_benchmark(
            runs_dir=args.runs_dir,
            run_id=args.run_id,
            load_iterations=max(1, args.load_iterations),
        )
    )
    print(artifacts["run_dir"])
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
