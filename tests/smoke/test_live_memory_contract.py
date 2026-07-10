from __future__ import annotations

import os
from dataclasses import asdict

import pytest

from eidolon_agent.app.benchmark.live_local_contract import (
    LiveLocalContractConfig,
    run_live_local_contract,
)

pytestmark = pytest.mark.smoke


@pytest.mark.asyncio
async def test_live_memory_contract_mcp_tools_nats_and_optional_readback() -> None:
    if os.environ.get("EIDOLON_AGENT_LIVE_MEMORY_CONTRACT") != "1":
        pytest.skip("set EIDOLON_AGENT_LIVE_MEMORY_CONTRACT=1 to run live memory contract smoke")

    report = await run_live_local_contract(
        LiveLocalContractConfig(
            mode="live-memory-smoke",
            include_agent_http=False,
            include_agent_admin=False,
            include_admin_gateway=False,
            include_memory=True,
            require_memory_readback=(
                os.environ.get("EIDOLON_AGENT_LIVE_MEMORY_READBACK") == "1"
            ),
            dependency_unavailable_status="skipped",
            memory_space_id=os.environ.get("EIDOLON_AGENT_LIVE_MEMORY_SPACE_ID"),
        )
    )

    skipped = [check for check in report.checks if check.status == "skipped"]
    if skipped:
        pytest.skip("; ".join(f"{check.name}: {check.summary}" for check in skipped))
    failed = [check for check in report.checks if check.status == "failed"]
    assert not failed, [asdict(check) for check in failed]
    assert report.passed is True
