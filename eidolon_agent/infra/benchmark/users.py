"""Shared benchmark identity guardrails.

Live benchmark scripts should never default to a human user. The default
identity is intentionally stable so reports and memory artifacts are isolated
from normal product usage.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_BENCHMARK_TENANT_ID = "default"
DEFAULT_BENCHMARK_USER_ID = "benchmark"


@dataclass(frozen=True)
class BenchmarkIdentity:
    tenant_id: str
    user_id: str


def is_benchmark_user(user_id: str) -> bool:
    normalized = user_id.strip().lower()
    return normalized == DEFAULT_BENCHMARK_USER_ID or normalized.startswith("benchmark-")


def resolve_benchmark_identity(
    *,
    tenant_id: str | None,
    user_id: str | None,
    allow_non_benchmark_user: bool = False,
) -> BenchmarkIdentity:
    resolved = BenchmarkIdentity(
        tenant_id=(tenant_id or DEFAULT_BENCHMARK_TENANT_ID).strip()
        or DEFAULT_BENCHMARK_TENANT_ID,
        user_id=(user_id or DEFAULT_BENCHMARK_USER_ID).strip()
        or DEFAULT_BENCHMARK_USER_ID,
    )
    if not is_benchmark_user(resolved.user_id) and not allow_non_benchmark_user:
        raise ValueError(
            "Benchmark scripts must use an isolated benchmark user by default. "
            f"Got user_id={resolved.user_id!r}. Use a user_id starting with "
            "'benchmark' or pass --allow-non-benchmark-user for explicit debugging."
        )
    return resolved
