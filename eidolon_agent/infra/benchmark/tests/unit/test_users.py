import pytest

from eidolon_agent.infra.benchmark.users import (
    DEFAULT_BENCHMARK_TENANT_ID,
    DEFAULT_BENCHMARK_USER_ID,
    is_benchmark_user,
    resolve_benchmark_identity,
)


def test_resolve_benchmark_identity_defaults_to_isolated_user() -> None:
    identity = resolve_benchmark_identity(
        tenant_id=None,
        user_id=None,
    )

    assert identity.tenant_id == DEFAULT_BENCHMARK_TENANT_ID
    assert identity.user_id == DEFAULT_BENCHMARK_USER_ID


def test_benchmark_user_prefix_is_allowed() -> None:
    assert is_benchmark_user("benchmark")
    assert is_benchmark_user("benchmark-voice")


def test_non_benchmark_user_requires_explicit_override() -> None:
    with pytest.raises(ValueError, match="allow-non-benchmark-user"):
        resolve_benchmark_identity(
            tenant_id="default",
            user_id="manson",
        )

    identity = resolve_benchmark_identity(
        tenant_id="default",
        user_id="manson",
        allow_non_benchmark_user=True,
    )
    assert identity.user_id == "manson"
