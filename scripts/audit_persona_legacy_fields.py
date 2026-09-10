"""Prepare a scoped migration review from an exported genome; never write Memory.

Input: {owner_id, companion_id, genome_id, genome: <canonical PersonaGenome>}.
The report retains provenance and defaults to the original Companion audience.
Relationship promises are not automatically converted into executable tasks.
"""

import argparse
import hashlib
import json
from pathlib import Path

from eidolon_sdk.biz.persona import normalize_persona_genome, persona_genome_hash


def migration_review(snapshot: dict) -> dict:
    identity = {key: snapshot[key] for key in ("owner_id", "companion_id", "genome_id")}
    if any(not isinstance(value, str) or not value.strip() for value in identity.values()):
        raise ValueError("migration requires explicit owner, companion and genome identifiers")
    genome = normalize_persona_genome(snapshot["genome"])
    candidates = []
    fields = {
        "relationship.pinned_facts": (genome.relationship.pinned_facts, "memory_fact_review"),
        "relationship.owner_preferences": (
            list(genome.relationship.owner_preferences.items()),
            "preference_classification_review",
        ),
        "relationship.commitments": (
            genome.relationship.commitments,
            "relationship_agreement_review",
        ),
    }
    for path, (values, destination) in fields.items():
        for index, value in enumerate(values):
            payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
            candidates.append(
                {
                    "source_path": f"{path}[{index}]",
                    "source_value": value,
                    "source_digest": hashlib.sha256(payload.encode()).hexdigest(),
                    "audience": f"companion:{identity['companion_id']}",
                    "candidate_destination": destination,
                    "decision": "needs_review",
                    "existing_memory_conflict": "not_checked",
                }
            )
    return {
        **identity,
        "genome_hash": persona_genome_hash(genome),
        "dry_run": True,
        "candidates": candidates,
        "write_performed": False,
        "cutover_allowed": False,
        "required_before_cutover": [
            "compare existing Memory by owner and audience",
            "review conflicts and scope",
            "record accepted target IDs",
            "verify readback before removing legacy reads",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = migration_review(json.loads(args.snapshot.read_text()))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
