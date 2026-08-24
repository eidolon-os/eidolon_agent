"""Admin reports router — read-only replay/realtime report browse."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from eidolon_agent.app.admin.tests.conftest import AUTHORITY_HEADERS
from eidolon_agent.app.admin.routers import reports as reports_router

pytestmark = pytest.mark.functional


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    root = tmp_path / "reports"
    (root / "replay").mkdir(parents=True)
    (root / "realtime").mkdir(parents=True)
    (root / "replay" / "latest.json").write_text(
        json.dumps(
            {
                "schema_version": "eidolon_agent.experience_replay_report.v1",
                "generated_at": "2026-06-04T10:00:00+00:00",
                "passed": True,
                "summary": {"scenario_count": 6, "failed": 0},
            }
        ),
        encoding="utf-8",
    )
    (root / "realtime" / "latest.json").write_text(
        json.dumps(
            {
                "schema_version": "eidolon_agent.realtime_guard_report.v1",
                "passed": False,
                "metrics": {"first_delta_p95_ms": 310},
            }
        ),
        encoding="utf-8",
    )
    api = FastAPI()
    api.state.reports_dir = root
    api.include_router(reports_router.router, prefix="/api/admin")
    return api


async def test_list_reports_returns_replay_and_realtime(app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=AUTHORITY_HEADERS
    ) as client:
        resp = await client.get("/api/admin/reports")

    assert resp.status_code == 200
    body = resp.json()
    by_id = {r["id"]: r for r in body["reports"]}
    assert by_id["replay/latest.json"]["passed"] is True
    assert by_id["replay/latest.json"]["summary"]["scenario_count"] == 6
    assert by_id["realtime/latest.json"]["metrics"]["first_delta_p95_ms"] == 310


async def test_get_report_returns_payload(app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=AUTHORITY_HEADERS
    ) as client:
        resp = await client.get("/api/admin/reports/replay/latest.json")

    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["id"] == "replay/latest.json"
    assert body["payload"]["summary"]["failed"] == 0


async def test_report_filename_rejects_path_traversal(app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=AUTHORITY_HEADERS
    ) as client:
        resp = await client.get("/api/admin/reports/replay/../secret.json")

    assert resp.status_code in {400, 404}
