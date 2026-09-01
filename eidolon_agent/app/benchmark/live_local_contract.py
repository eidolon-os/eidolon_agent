"""Live-local contract harness for the canonical persona stack.

The harness verifies public boundaries only:

* Agent HTTP/admin endpoints over HTTP.
* Admin gateway endpoints over HTTP.
* Memory discovery, MCP tools, and NATS write/readback through Agent adapters.

It intentionally does not import eidolon_memory internals or duplicate the
admin dev-stack process manager.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx

from eidolon_agent.config import load_settings
from eidolon_agent.core.errors import MemoryUnavailableError
from eidolon_agent.domain.history import HistoryFanout
from eidolon_agent.infra.events import NatsEventBus
from eidolon_agent.infra.memory import (
    McpClientPool,
    build_initial_memory_routes,
)

CheckStatus = Literal["passed", "failed", "skipped"]
DependencyUnavailableStatus = Literal["failed", "skipped"]


@dataclass(slots=True)
class ContractCheck:
    name: str
    status: CheckStatus
    required: bool
    summary: str
    elapsed_ms: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LiveLocalContractReport:
    schema_version: str
    generated_at: str
    mode: str
    passed: bool
    summary: dict[str, Any]
    checks: list[ContractCheck]
    elapsed_ms: float


@dataclass(slots=True)
class LiveLocalContractConfig:
    mode: str = "live-local"
    agent_http_base: str = "http://127.0.0.1:8180"
    agent_admin_base: str = "http://127.0.0.1:8081"
    admin_gateway_base: str | None = "http://127.0.0.1:9000"
    include_agent_http: bool = True
    include_agent_admin: bool = True
    include_admin_gateway: bool = True
    include_memory: bool = True
    require_memory_readback: bool = True
    dependency_unavailable_status: DependencyUnavailableStatus = "failed"
    memory_space_id: str | None = None
    memory_owner_id: str | None = None
    memory_companion_id: str | None = None
    memory_isolation_companion_ids: tuple[str, ...] = ()
    memory_recall_samples: int = 0
    memory_recall_p95_budget_ms: float | None = None
    require_memory_cleanup: bool = True
    timeout_s: float = 5.0
    memory_route_timeout_s: float = 0.0
    memory_readback_timeout_s: float = 30.0
    memory_readback_poll_s: float = 0.5
    run_product_acceptance: bool = False
    product_acceptance_work_dir: Path = Path("/tmp/eidolon-product-acceptance")
    product_acceptance_sqlite_path: Path | None = None

    def __post_init__(self) -> None:
        if self.dependency_unavailable_status not in {"failed", "skipped"}:
            raise ValueError("dependency_unavailable_status must be 'failed' or 'skipped'")


async def run_live_local_contract(
    config: LiveLocalContractConfig | None = None,
) -> LiveLocalContractReport:
    cfg = config or LiveLocalContractConfig()
    started = time.perf_counter()
    checks: list[ContractCheck] = []
    if cfg.include_agent_http:
        checks.append(
            await _http_json_check(
                name="agent_http_readyz",
                url=_join_url(cfg.agent_http_base, "/readyz"),
                required=True,
                timeout_s=cfg.timeout_s,
                expected_status=200,
                expected_json={"status": "ready"},
                unavailable_status=cfg.dependency_unavailable_status,
                hint="Start eidolon_agent or the admin dev stack.",
            )
        )
    if cfg.include_agent_admin:
        checks.append(
            await _http_json_check(
                name="agent_admin_openapi",
                url=_join_url(cfg.agent_admin_base, "/api/openapi.json"),
                required=True,
                timeout_s=cfg.timeout_s,
                expected_status=200,
                expected_json_path=("info", "title"),
                expected_json_value="eidolon-agent admin",
                unavailable_status=cfg.dependency_unavailable_status,
                hint="Start eidolon_agent admin HTTP on the configured admin port.",
            )
        )
    if cfg.include_admin_gateway and cfg.admin_gateway_base:
        checks.append(
            await _http_json_check(
                name="admin_gateway_services",
                url=_join_url(cfg.admin_gateway_base, "/api/services"),
                required=True,
                timeout_s=cfg.timeout_s,
                expected_status=200,
                expected_json_key="services",
                unavailable_status=cfg.dependency_unavailable_status,
                hint="Start eidolon_admin with deploy/dev/run_all.sh start.",
            )
        )
        checks.append(
            await _http_json_check(
                name="admin_gateway_system_health",
                url=_join_url(cfg.admin_gateway_base, "/api/system/health"),
                required=True,
                timeout_s=max(cfg.timeout_s, 10.0),
                expected_status=200,
                expected_json_key="services",
                unavailable_status=cfg.dependency_unavailable_status,
                hint="Admin gateway is up but system health is unavailable.",
            )
        )

    if cfg.include_memory:
        checks.extend(
            await _memory_contract_checks(
                cfg,
                selected_memory_space_id=cfg.memory_space_id,
            )
        )

    if cfg.run_product_acceptance:
        checks.append(await _product_acceptance_check(cfg))

    return _build_report(cfg=cfg, checks=checks, started=started)


def _build_report(
    *,
    cfg: LiveLocalContractConfig,
    checks: list[ContractCheck],
    started: float,
) -> LiveLocalContractReport:
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    required = [check for check in checks if check.required]
    failed_required = [check for check in required if check.status == "failed"]
    skipped_required = [check for check in required if check.status == "skipped"]
    passed = not failed_required and not skipped_required
    return LiveLocalContractReport(
        schema_version="eidolon_agent.live_local_contract_report.v1",
        generated_at=datetime.now(UTC).isoformat(),
        mode=cfg.mode,
        passed=passed,
        summary={
            "checks": len(checks),
            "passed": sum(1 for check in checks if check.status == "passed"),
            "failed": sum(1 for check in checks if check.status == "failed"),
            "skipped": sum(1 for check in checks if check.status == "skipped"),
            "required": len(required),
            "failed_required": [check.name for check in failed_required],
            "skipped_required": [check.name for check in skipped_required],
        },
        checks=checks,
        elapsed_ms=elapsed_ms,
    )


async def _http_json_check(
    *,
    name: str,
    url: str,
    required: bool,
    timeout_s: float,
    expected_status: int,
    unavailable_status: DependencyUnavailableStatus,
    hint: str,
    expected_json: dict[str, Any] | None = None,
    expected_json_key: str | None = None,
    expected_json_path: tuple[str, ...] | None = None,
    expected_json_value: Any = None,
) -> ContractCheck:
    started = time.perf_counter()
    deadline = started + max(timeout_s, 0.001)
    attempts = 0
    while True:
        attempts += 1
        remaining_s = max(0.001, deadline - time.perf_counter())
        try:
            async with httpx.AsyncClient(
                timeout=remaining_s,
                follow_redirects=True,
                trust_env=False,
            ) as client:
                response = await client.get(url)
            break
        except Exception as exc:
            if time.perf_counter() >= deadline:
                return _check(
                    name=name,
                    status=unavailable_status,
                    required=required,
                    started=started,
                    summary=f"{type(exc).__name__}: {exc}",
                    details={"url": url, "hint": hint, "attempts": attempts},
                )
            await asyncio.sleep(min(0.25, max(0.0, deadline - time.perf_counter())))

    details: dict[str, Any] = {
        "url": url,
        "status_code": response.status_code,
        "attempts": attempts,
    }
    if response.status_code != expected_status:
        details["body_preview"] = response.text[:500]
        return _check(
            name=name,
            status="failed",
            required=required,
            started=started,
            summary=f"expected HTTP {expected_status}, got {response.status_code}",
            details=details,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        return _check(
            name=name,
            status="failed",
            required=required,
            started=started,
            summary=f"response is not JSON: {exc}",
            details=details,
        )

    if expected_json is not None and payload != expected_json:
        details["payload"] = payload
        return _check(
            name=name,
            status="failed",
            required=required,
            started=started,
            summary="JSON payload mismatch",
            details=details,
        )
    if expected_json_key is not None and expected_json_key not in payload:
        details["payload_keys"] = sorted(payload) if isinstance(payload, dict) else []
        return _check(
            name=name,
            status="failed",
            required=required,
            started=started,
            summary=f"missing JSON key {expected_json_key!r}",
            details=details,
        )
    if expected_json_path is not None:
        actual = _get_path(payload, expected_json_path)
        if actual != expected_json_value:
            details["actual"] = actual
            return _check(
                name=name,
                status="failed",
                required=required,
                started=started,
                summary=f"expected {'.'.join(expected_json_path)}={expected_json_value!r}",
                details=details,
            )

    return _check(
        name=name,
        status="passed",
        required=required,
        started=started,
        summary="ok",
        details=details,
    )


async def _memory_contract_checks(
    cfg: LiveLocalContractConfig,
    *,
    selected_memory_space_id: str | None = None,
) -> list[ContractCheck]:
    checks: list[ContractCheck] = []
    started = time.perf_counter()
    owner_id = str(cfg.memory_owner_id or "").strip()
    companion_id = str(cfg.memory_companion_id or "").strip()
    if not owner_id or not companion_id:
        return [
            _check(
                name="memory_test_identity",
                status="failed",
                required=True,
                started=started,
                summary="memory_owner_id and memory_companion_id are required",
                details={
                    "owner_id_present": bool(owner_id),
                    "companion_id_present": bool(companion_id),
                },
            )
        ]
    deadline = time.perf_counter() + max(0.0, cfg.memory_route_timeout_s)
    last_details: dict[str, Any] | None = None
    try:
        while True:
            settings = load_settings()
            routes, effective_nats_url, refresher = await build_initial_memory_routes(
                memory=settings.memory,
                nats=settings.nats,
                log_initial_fetch_exception=False,
            )
            if refresher is not None:
                await refresher.stop()
            memory_space_ids = await routes.memory_space_ids()
            source = await routes.source()
            last_details = {
                "source": source,
                "effective_nats_url": effective_nats_url,
                "memory_space_ids": memory_space_ids,
            }
            if memory_space_ids and (
                not selected_memory_space_id or selected_memory_space_id in memory_space_ids
            ):
                break
            if time.perf_counter() >= deadline:
                break
            await asyncio.sleep(min(cfg.memory_readback_poll_s, 1.0))
    except Exception as exc:
        return [
            _check(
                name="memory_discovery",
                status=cfg.dependency_unavailable_status,
                required=True,
                started=started,
                summary=f"{type(exc).__name__}: {exc}",
                details={"hint": "Check eidolon_agent config and eidolon_memory discovery."},
            )
        ]

    details = last_details or {
        "source": source,
        "effective_nats_url": effective_nats_url,
        "memory_space_ids": memory_space_ids,
    }
    if cfg.memory_route_timeout_s > 0:
        details["route_timeout_s"] = cfg.memory_route_timeout_s
    if not memory_space_ids:
        return [
            _check(
                name="memory_discovery",
                status=cfg.dependency_unavailable_status,
                required=True,
                started=started,
                summary="no enabled reachable memory routes",
                details=details,
            )
        ]

    memory_space_id = (selected_memory_space_id or "").strip() or memory_space_ids[0]
    if memory_space_id not in memory_space_ids:
        checks.append(
            _check(
                name="memory_discovery",
                status="failed",
                required=True,
                started=started,
                summary=f"requested memory space {memory_space_id!r} not discovered",
                details=details,
            )
        )
        return checks

    checks.append(
        _check(
            name="memory_discovery",
            status="passed",
            required=True,
            started=started,
            summary="ok",
            details={**details, "selected_memory_space_id": memory_space_id},
        )
    )

    pool = McpClientPool(routes=routes)
    bus = NatsEventBus(
        effective_nats_url,
        creds_path=str(settings.nats.creds_path) if settings.nats.creds_path else None,
    )
    try:
        agent_session, agent_tools = await _probe_memory_agent_tools(
            pool=pool,
            memory_space_id=memory_space_id,
            timeout_s=cfg.timeout_s,
            unavailable_status=cfg.dependency_unavailable_status,
        )
        checks.append(agent_tools)
        if agent_session is None or agent_tools.status != "passed":
            return checks
        ops_session, ops_tools = await _probe_memory_ops_tools(
            pool=pool,
            memory_space_id=memory_space_id,
            timeout_s=cfg.timeout_s,
            unavailable_status=cfg.dependency_unavailable_status,
        )
        checks.append(ops_tools)
        if ops_session is None or ops_tools.status != "passed":
            return checks
        advertised_agent = set(agent_tools.details.get("tool_names") or [])

        checks.append(await _memory_status_check(ops_session, memory_space_id, cfg))
        publish_check, turn_id = await _memory_publish_check(
            bus=bus,
            routes=routes,
            memory_space_id=memory_space_id,
            owner_id=owner_id,
            companion_id=companion_id,
            cfg=cfg,
        )
        checks.append(publish_check)
        if publish_check.status == "passed":
            readback = await _memory_readback_check(
                pool=pool,
                tool_names=advertised_agent,
                memory_space_id=memory_space_id,
                marker=turn_id,
                owner_id=owner_id,
                companion_id=companion_id,
                cfg=cfg,
            )
            checks.append(readback)
            if readback.status == "passed" and cfg.memory_isolation_companion_ids:
                checks.append(
                    await _memory_scope_isolation_check(
                        pool=pool,
                        memory_space_id=memory_space_id,
                        marker=turn_id,
                        owner_id=owner_id,
                        primary_companion_id=companion_id,
                        isolation_companion_ids=cfg.memory_isolation_companion_ids,
                        cfg=cfg,
                    )
                )
            if readback.status == "passed" and cfg.memory_recall_samples > 0:
                checks.append(
                    await _memory_recall_latency_check(
                        pool=pool,
                        memory_space_id=memory_space_id,
                        marker=turn_id,
                        owner_id=owner_id,
                        companion_id=companion_id,
                        cfg=cfg,
                    )
                )
            checks.append(
                await _memory_cleanup_check(
                    pool=pool,
                    ops_session=ops_session,
                    memory_space_id=memory_space_id,
                    owner_id=owner_id,
                    companion_id=companion_id,
                    marker=turn_id,
                    cfg=cfg,
                )
            )
        return checks
    finally:
        await pool.close_all()
        await bus.close()


async def _probe_memory_agent_tools(
    *,
    pool: McpClientPool,
    memory_space_id: str,
    timeout_s: float,
    unavailable_status: DependencyUnavailableStatus,
) -> tuple[Any | None, ContractCheck]:
    started = time.perf_counter()
    try:
        session = await pool.session_for(memory_space_id)
        tool_names = await asyncio.wait_for(session.tool_names(), timeout=timeout_s)
    except MemoryUnavailableError as exc:
        return None, _check(
            name="memory_agent_mcp_tools",
            status=unavailable_status,
            required=True,
            started=started,
            summary=str(exc),
            details={"memory_space_id": memory_space_id},
        )
    except Exception as exc:
        return None, _check(
            name="memory_agent_mcp_tools",
            status=unavailable_status,
            required=True,
            started=started,
            summary=f"{type(exc).__name__}: {exc}",
            details={"memory_space_id": memory_space_id},
        )
    if tool_names is None:
        return session, _check(
            name="memory_agent_mcp_tools",
            status=unavailable_status,
            required=True,
            started=started,
            summary="MCP list_tools did not return capabilities",
            details={"memory_space_id": memory_space_id},
        )
    advertised = sorted(tool_names)
    required = {
        "eidolon_memory_active_commitments",
        "eidolon_memory_recall_context",
        "eidolon_memory_search",
    }
    operator_only = {"eidolon_memory_get_by_source_turn", "eidolon_memory_status"}
    missing = required - set(tool_names)
    leaked = operator_only & set(tool_names)
    if missing or leaked:
        return session, _check(
            name="memory_agent_mcp_tools",
            status="failed",
            required=True,
            started=started,
            summary="agent MCP capability boundary is invalid",
            details={
                "memory_space_id": memory_space_id,
                "tool_names": advertised,
                "missing": sorted(missing),
                "operator_tools_exposed": sorted(leaked),
            },
        )
    return session, _check(
        name="memory_agent_mcp_tools",
        status="passed",
        required=True,
        started=started,
        summary="ok",
        details={"memory_space_id": memory_space_id, "tool_names": advertised},
    )


async def _probe_memory_ops_tools(
    *,
    pool: McpClientPool,
    memory_space_id: str,
    timeout_s: float,
    unavailable_status: DependencyUnavailableStatus,
) -> tuple[Any | None, ContractCheck]:
    """Verify the operator surface separately from the Agent read surface."""

    started = time.perf_counter()
    try:
        session = await pool.write_session_for(memory_space_id)
        tool_names = await asyncio.wait_for(session.tool_names(), timeout=timeout_s)
    except MemoryUnavailableError as exc:
        return None, _check(
            name="memory_ops_mcp_tools",
            status=unavailable_status,
            required=True,
            started=started,
            summary=str(exc),
            details={"memory_space_id": memory_space_id},
        )
    except Exception as exc:
        return None, _check(
            name="memory_ops_mcp_tools",
            status=unavailable_status,
            required=True,
            started=started,
            summary=f"{type(exc).__name__}: {exc}",
            details={"memory_space_id": memory_space_id},
        )
    if tool_names is None:
        return session, _check(
            name="memory_ops_mcp_tools",
            status=unavailable_status,
            required=True,
            started=started,
            summary="operator MCP list_tools did not return capabilities",
            details={"memory_space_id": memory_space_id},
        )
    advertised = sorted(tool_names)
    missing = {
        "eidolon_memory_forget_source_event",
        "eidolon_memory_get_by_source_turn",
        "eidolon_memory_status",
    } - set(tool_names)
    if missing:
        return session, _check(
            name="memory_ops_mcp_tools",
            status="failed",
            required=True,
            started=started,
            summary="required operator MCP tools are missing",
            details={
                "memory_space_id": memory_space_id,
                "tool_names": advertised,
                "missing": sorted(missing),
            },
        )
    return session, _check(
        name="memory_ops_mcp_tools",
        status="passed",
        required=True,
        started=started,
        summary="ok",
        details={"memory_space_id": memory_space_id, "tool_names": advertised},
    )


async def _memory_status_check(
    session: Any,
    memory_space_id: str,
    cfg: LiveLocalContractConfig,
) -> ContractCheck:
    started = time.perf_counter()
    try:
        status = await asyncio.wait_for(
            session.call_tool("eidolon_memory_status", {}),
            timeout=cfg.timeout_s,
        )
    except MemoryUnavailableError as exc:
        return _check(
            name="memory_mcp_status",
            status=cfg.dependency_unavailable_status,
            required=True,
            started=started,
            summary=str(exc),
            details={"memory_space_id": memory_space_id},
        )
    except Exception as exc:
        return _check(
            name="memory_mcp_status",
            status="failed",
            required=True,
            started=started,
            summary=f"{type(exc).__name__}: {exc}",
            details={"memory_space_id": memory_space_id},
        )
    ready = bool(status.get("ready", True)) if isinstance(status, dict) else False
    actual_space = str(status.get("memory_space_id") or "") if isinstance(status, dict) else ""
    if not ready or (actual_space and actual_space != memory_space_id):
        return _check(
            name="memory_mcp_status",
            status="failed",
            required=True,
            started=started,
            summary="memory status is not ready or identity mismatched",
            details={
                "memory_space_id": memory_space_id,
                "status": status,
            },
        )
    return _check(
        name="memory_mcp_status",
        status="passed",
        required=True,
        started=started,
        summary="ok",
        details={"memory_space_id": memory_space_id, "status": status},
    )


async def _memory_publish_check(
    *,
    bus: NatsEventBus,
    routes: Any,
    memory_space_id: str,
    owner_id: str,
    companion_id: str,
    cfg: LiveLocalContractConfig,
) -> tuple[ContractCheck, str]:
    started = time.perf_counter()
    turn_id = f"live-local-contract-{uuid4().hex}"
    fanout = HistoryFanout(event_bus=bus, memory_routes=routes)
    try:
        status = await fanout.publish_turn(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_space_id,
            device_id=None,
            session_id="live-local-contract",
            turn_id=turn_id,
            user_text=_memory_contract_owner_text(),
            assistant_text="Acquired 很适合用来了解科技公司和商业史。",
            timestamp_iso=datetime.now(UTC).isoformat(),
            trace_id=turn_id,
            metadata={
                "source": "eidolon-agent-live-local-contract",
                "purpose": "memory-contract-readback",
            },
        )
    except Exception as exc:
        return _check(
            name="memory_nats_publish",
            status="failed",
            required=True,
            started=started,
            summary=f"{type(exc).__name__}: {exc}",
            details={"memory_space_id": memory_space_id, "turn_id": turn_id},
        ), turn_id
    if status.state != "published":
        return _check(
            name="memory_nats_publish",
            status=cfg.dependency_unavailable_status,
            required=True,
            started=started,
            summary=status.error or status.state,
            details={
                "memory_space_id": memory_space_id,
                "turn_id": turn_id,
                "fanout_state": status.state,
            },
        ), turn_id
    return _check(
        name="memory_nats_publish",
        status="passed",
        required=True,
        started=started,
        summary="ok",
        details={
            "memory_space_id": memory_space_id,
            "turn_id": turn_id,
            "fanout_state": status.state,
        },
    ), turn_id


def _memory_contract_owner_text() -> str:
    return "我最喜欢的播客是 Acquired 半导体播客。"


def _memory_contract_recall_query() -> str:
    return "我最喜欢的播客是什么？"


async def _memory_readback_check(
    *,
    pool: McpClientPool,
    tool_names: set[str],
    memory_space_id: str,
    marker: str,
    owner_id: str,
    companion_id: str,
    cfg: LiveLocalContractConfig,
) -> ContractCheck:
    started = time.perf_counter()
    required = cfg.require_memory_readback
    if "eidolon_memory_search" not in tool_names:
        return _check(
            name="memory_nats_readback",
            status="failed" if required else "skipped",
            required=required,
            started=started,
            summary="eidolon_memory_search is not advertised",
            details={"memory_space_id": memory_space_id, "marker": marker},
        )

    deadline = time.perf_counter() + cfg.memory_readback_timeout_s
    last_payload: Any = None
    last_error: str | None = None
    last_dependency_unavailable = False
    while time.perf_counter() < deadline:
        remaining_s = deadline - time.perf_counter()
        if remaining_s <= 0:
            break
        session: Any | None = None
        try:
            session = await pool.session_for(memory_space_id)
            payload = await _call_mcp_tool_with_timeout(
                session,
                "eidolon_memory_search",
                {
                    "query": _memory_contract_recall_query(),
                    "context": _memory_canary_context(
                        memory_space_id=memory_space_id,
                        owner_id=owner_id,
                        companion_id=companion_id,
                    ),
                    "top_k": 10,
                },
                timeout_s=max(0.001, min(cfg.timeout_s, remaining_s)),
            )
        except TimeoutError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            last_dependency_unavailable = False
            await _drop_agent_mcp_session(pool, memory_space_id, session)
            await asyncio.sleep(
                min(cfg.memory_readback_poll_s, max(0.0, deadline - time.perf_counter()))
            )
            continue
        except MemoryUnavailableError as exc:
            last_error = str(exc)
            last_dependency_unavailable = True
            await _drop_agent_mcp_session(pool, memory_space_id, session)
            await asyncio.sleep(
                min(cfg.memory_readback_poll_s, max(0.0, deadline - time.perf_counter()))
            )
            continue
        except Exception as exc:
            return _check(
                name="memory_nats_readback",
                status="failed",
                required=required,
                started=started,
                summary=f"{type(exc).__name__}: {exc}",
                details={"memory_space_id": memory_space_id, "marker": marker},
            )
        last_payload = payload
        last_error = None
        last_dependency_unavailable = False
        matching = _memory_records_for_source_event(payload, marker)
        if matching:
            record = matching[0]
            return _check(
                name="memory_nats_readback",
                status="passed",
                required=required,
                started=started,
                summary="ok",
                details={
                    "memory_space_id": memory_space_id,
                    "marker": marker,
                    "record_key": record.get("key") or record.get("id"),
                },
            )
        await asyncio.sleep(
            min(cfg.memory_readback_poll_s, max(0.0, deadline - time.perf_counter()))
        )

    status: CheckStatus
    if last_dependency_unavailable:
        status = cfg.dependency_unavailable_status
    else:
        status = "failed" if required else "skipped"

    return _check(
        name="memory_nats_readback",
        status=status,
        required=required,
        started=started,
        summary="timed out waiting for memory readback",
        details={
            "memory_space_id": memory_space_id,
            "marker": marker,
            "timeout_s": cfg.memory_readback_timeout_s,
            "last_payload": last_payload,
            "last_error": last_error,
        },
    )


async def _memory_cleanup_check(
    *,
    pool: McpClientPool,
    ops_session: Any,
    memory_space_id: str,
    owner_id: str,
    companion_id: str,
    marker: str,
    cfg: LiveLocalContractConfig,
) -> ContractCheck:
    """Delete the canary through the product privacy workflow and prove absence."""

    started = time.perf_counter()
    if not cfg.require_memory_cleanup:
        return _check(
            name="memory_marker_cleanup",
            status="skipped",
            required=False,
            started=started,
            summary="cleanup disabled explicitly",
            details={"memory_space_id": memory_space_id, "marker": marker},
        )
    details: dict[str, Any] = {
        "memory_space_id": memory_space_id,
        "marker": marker,
    }
    cleanup_deadline = time.perf_counter() + max(cfg.memory_readback_timeout_s, cfg.timeout_s)
    cleanup: Any = None
    cleanup_error: str | None = None
    session = ops_session
    while time.perf_counter() < cleanup_deadline:
        remaining_s = cleanup_deadline - time.perf_counter()
        wait_s = max(0.001, min(10.0, remaining_s))
        try:
            cleanup = await _call_mcp_tool_with_timeout(
                session,
                "eidolon_memory_forget_source_event",
                {
                    "source_event_id": marker,
                    "wait_applied_seconds": wait_s,
                },
                timeout_s=max(cfg.timeout_s, wait_s + 2.0),
            )
            if not isinstance(cleanup, dict):
                raise RuntimeError("source-event cleanup returned an invalid payload")
            details["cleanup_status"] = cleanup.get("status")
            details["cleanup_request_id"] = cleanup.get("request_id")
            details["source_event_tombstoned"] = cleanup.get("source_event_tombstoned")
            if (
                cleanup.get("status") == "applied"
                and cleanup.get("source_event_tombstoned") is True
            ):
                cleanup_error = None
                break
            cleanup_error = "source-event cleanup is not yet applied and tombstoned"
        except Exception as exc:
            cleanup_error = f"{type(exc).__name__}: {exc}"
            await _drop_ops_mcp_session(pool, memory_space_id, session)
            try:
                session = await pool.write_session_for(memory_space_id)
            except Exception as reconnect_exc:
                cleanup_error = f"{type(reconnect_exc).__name__}: {reconnect_exc}"
        await asyncio.sleep(
            min(cfg.memory_readback_poll_s, max(0.0, cleanup_deadline - time.perf_counter()))
        )
    if cleanup_error is not None:
        details["cleanup_error"] = cleanup_error
        return _check(
            name="memory_marker_cleanup",
            status="failed",
            required=True,
            started=started,
            summary=cleanup_error,
            details=details,
        )

    deadline = time.perf_counter() + cfg.memory_readback_timeout_s
    last_payload: Any = None
    while time.perf_counter() < deadline:
        try:
            session = await pool.session_for(memory_space_id)
            last_payload = await _call_mcp_tool_with_timeout(
                session,
                "eidolon_memory_search",
                {
                    "query": _memory_contract_recall_query(),
                    "context": _memory_canary_context(
                        memory_space_id=memory_space_id,
                        owner_id=owner_id,
                        companion_id=companion_id,
                    ),
                    "top_k": 10,
                },
                timeout_s=cfg.timeout_s,
            )
            if not _memory_records_for_source_event(last_payload, marker):
                return _check(
                    name="memory_marker_cleanup",
                    status="passed",
                    required=True,
                    started=started,
                    summary="source event tombstoned and no longer readable",
                    details=details,
                )
        except Exception as exc:
            details["last_verify_error"] = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(cfg.memory_readback_poll_s)

    details["last_verify_payload"] = last_payload
    return _check(
        name="memory_marker_cleanup",
        status="failed",
        required=True,
        started=started,
        summary="marker remained readable after applied forget",
        details=details,
    )


def _memory_canary_context(
    *,
    memory_space_id: str,
    owner_id: str,
    companion_id: str,
) -> dict[str, str]:
    """Use the same authoritative scope on write, readback, and deletion proof."""

    return {
        "memory_realm_id": memory_space_id,
        "memory_space_id": memory_space_id,
        "owner_id": owner_id,
        "companion_id": companion_id,
        "device_id": "agent-live-contract",
        "session_id": "live-local-contract",
    }


def _memory_records_for_source_event(
    payload: Any,
    source_event_id: str,
) -> list[dict[str, Any]]:
    """Correlate recall with the durable source event, independent of wording."""

    records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        return []
    return [
        record
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("metadata"), dict)
        and record["metadata"].get("source_event_id") == source_event_id
    ]


async def _memory_search_for_companion(
    *,
    pool: McpClientPool,
    memory_space_id: str,
    owner_id: str,
    companion_id: str,
    cfg: LiveLocalContractConfig,
) -> tuple[Any, float]:
    session = await pool.session_for(memory_space_id)
    started = time.perf_counter()
    payload = await _call_mcp_tool_with_timeout(
        session,
        "eidolon_memory_search",
        {
            "query": _memory_contract_recall_query(),
            "context": _memory_canary_context(
                memory_space_id=memory_space_id,
                owner_id=owner_id,
                companion_id=companion_id,
            ),
            "top_k": 10,
        },
        timeout_s=cfg.timeout_s,
    )
    return payload, (time.perf_counter() - started) * 1000


async def _memory_scope_isolation_check(
    *,
    pool: McpClientPool,
    memory_space_id: str,
    marker: str,
    owner_id: str,
    primary_companion_id: str,
    isolation_companion_ids: tuple[str, ...],
    cfg: LiveLocalContractConfig,
) -> ContractCheck:
    """Prove one natural turn is visible only in its authoritative Companion scope."""

    started = time.perf_counter()
    companion_ids = tuple(dict.fromkeys((primary_companion_id, *isolation_companion_ids)))
    results: dict[str, dict[str, Any]] = {}
    try:
        for companion_id in companion_ids:
            payload, elapsed_ms = await _memory_search_for_companion(
                pool=pool,
                memory_space_id=memory_space_id,
                owner_id=owner_id,
                companion_id=companion_id,
                cfg=cfg,
            )
            matching = _memory_records_for_source_event(payload, marker)
            results[companion_id] = {
                "matching_source_events": len(matching),
                "elapsed_ms": round(elapsed_ms, 3),
            }
    except Exception as exc:
        return _check(
            name="memory_companion_scope_isolation",
            status="failed",
            required=True,
            started=started,
            summary=f"{type(exc).__name__}: {exc}",
            details={"marker": marker, "results": results},
        )

    primary_visible = results[primary_companion_id]["matching_source_events"] > 0
    leaked_to = [
        companion_id
        for companion_id in isolation_companion_ids
        if results.get(companion_id, {}).get("matching_source_events", 0) > 0
    ]
    passed = primary_visible and not leaked_to
    return _check(
        name="memory_companion_scope_isolation",
        status="passed" if passed else "failed",
        required=True,
        started=started,
        summary="primary visible and peers isolated" if passed else "scope isolation violated",
        details={
            "marker": marker,
            "primary_companion_id": primary_companion_id,
            "leaked_to": leaked_to,
            "results": results,
        },
    )


def _nearest_rank_percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


async def _memory_recall_latency_check(
    *,
    pool: McpClientPool,
    memory_space_id: str,
    marker: str,
    owner_id: str,
    companion_id: str,
    cfg: LiveLocalContractConfig,
) -> ContractCheck:
    """Measure the real Agent-facing MCP read path, including scope hydration."""

    started = time.perf_counter()
    samples: list[float] = []
    missing = 0
    try:
        for _ in range(max(1, cfg.memory_recall_samples)):
            payload, elapsed_ms = await _memory_search_for_companion(
                pool=pool,
                memory_space_id=memory_space_id,
                owner_id=owner_id,
                companion_id=companion_id,
                cfg=cfg,
            )
            samples.append(elapsed_ms)
            if not _memory_records_for_source_event(payload, marker):
                missing += 1
    except Exception as exc:
        return _check(
            name="memory_agent_recall_latency",
            status="failed",
            required=True,
            started=started,
            summary=f"{type(exc).__name__}: {exc}",
            details={"samples_completed": len(samples)},
        )

    p50 = _nearest_rank_percentile(samples, 0.50)
    p95 = _nearest_rank_percentile(samples, 0.95)
    p99 = _nearest_rank_percentile(samples, 0.99)
    budget = cfg.memory_recall_p95_budget_ms
    passed = missing == 0 and (budget is None or p95 <= budget)
    return _check(
        name="memory_agent_recall_latency",
        status="passed" if passed else "failed",
        required=True,
        started=started,
        summary="recall samples visible and within budget" if passed else "recall SLO violated",
        details={
            "samples": len(samples),
            "missing_source_event_samples": missing,
            "p50_ms": round(p50, 3),
            "p95_ms": round(p95, 3),
            "p99_ms": round(p99, 3),
            "max_ms": round(max(samples), 3),
            "p95_budget_ms": budget,
        },
    )


async def _call_mcp_tool_with_timeout(
    session: Any,
    name: str,
    arguments: dict[str, Any],
    *,
    timeout_s: float,
) -> Any:
    task = asyncio.create_task(session.call_tool(name, arguments))
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_s)
    except asyncio.CancelledError as exc:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        raise TimeoutError(f"MCP call cancelled before completion: {exc}") from exc
    if task in done:
        try:
            return task.result()
        except asyncio.CancelledError as exc:
            raise TimeoutError(f"MCP call cancelled before completion: {exc}") from exc

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    raise TimeoutError()


async def _drop_ops_mcp_session(
    pool: McpClientPool,
    memory_space_id: str,
    session: Any | None,
) -> None:
    if session is None:
        return
    try:
        await pool.drop_write_session(memory_space_id, session=session)
    except Exception:
        pass


async def _drop_agent_mcp_session(
    pool: McpClientPool,
    memory_space_id: str,
    session: Any | None,
) -> None:
    if session is None:
        return
    try:
        await pool.drop_session(memory_space_id, session=session)
    except Exception:
        pass


async def _product_acceptance_check(cfg: LiveLocalContractConfig) -> ContractCheck:
    from eidolon_agent.app.benchmark.product_acceptance import (
        ProductAcceptanceUnavailable,
        run_product_acceptance_profile,
    )

    started = time.perf_counter()
    try:
        result = await run_product_acceptance_profile(
            work_dir=cfg.product_acceptance_work_dir,
            sqlite_path=cfg.product_acceptance_sqlite_path,
        )
    except ProductAcceptanceUnavailable as exc:
        return _check(
            name="product_acceptance",
            status=cfg.dependency_unavailable_status,
            required=True,
            started=started,
            summary=str(exc),
        )
    except Exception as exc:
        return _check(
            name="product_acceptance",
            status="failed",
            required=True,
            started=started,
            summary=f"{type(exc).__name__}: {exc}",
        )
    return _check(
        name="product_acceptance",
        status="passed" if result.passed else "failed",
        required=True,
        started=started,
        summary="ok" if result.passed else "product acceptance failed",
        details=asdict(result),
    )


def _check(
    *,
    name: str,
    status: CheckStatus,
    required: bool,
    started: float,
    summary: str,
    details: dict[str, Any] | None = None,
) -> ContractCheck:
    return ContractCheck(
        name=name,
        status=status,
        required=required,
        summary=summary,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        details=details or {},
    )


def _get_path(payload: Any, path: tuple[str, ...]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def report_to_dict(report: LiveLocalContractReport) -> dict[str, Any]:
    return asdict(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="live-local-contract")
    parser.add_argument("--agent-http", default="http://127.0.0.1:8180")
    parser.add_argument("--agent-admin", default="http://127.0.0.1:8081")
    parser.add_argument("--admin-gateway", default="http://127.0.0.1:9000")
    parser.add_argument(
        "--memory-space-id", default=os.getenv("EIDOLON_AGENT_LIVE_MEMORY_SPACE_ID", "")
    )
    parser.add_argument(
        "--memory-owner-id", default=os.getenv("EIDOLON_AGENT_LIVE_MEMORY_OWNER_ID", "")
    )
    parser.add_argument(
        "--memory-companion-id",
        default=os.getenv("EIDOLON_AGENT_LIVE_MEMORY_COMPANION_ID", ""),
    )
    parser.add_argument(
        "--memory-isolation-companion-id",
        action="append",
        default=[],
        help="Companion ID that must not see the primary canary; repeatable.",
    )
    parser.add_argument("--memory-recall-samples", type=int, default=0)
    parser.add_argument("--memory-recall-p95-budget-ms", type=float, default=None)
    parser.add_argument("--timeout-s", type=float, default=5.0)
    parser.add_argument("--memory-route-timeout-s", type=float, default=0.0)
    parser.add_argument("--memory-readback-timeout-s", type=float, default=30.0)
    parser.add_argument("--no-agent", action="store_true")
    parser.add_argument("--no-admin-gateway", action="store_true")
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--no-memory-readback", action="store_true")
    parser.add_argument("--no-memory-cleanup", action="store_true")
    parser.add_argument("--product-acceptance", action="store_true")
    parser.add_argument(
        "--product-acceptance-work-dir", type=Path, default=Path("/tmp/eidolon-product-acceptance")
    )
    parser.add_argument("--sqlite-path", type=Path, default=None)
    args = parser.parse_args(argv)

    report = asyncio.run(
        run_live_local_contract(
            LiveLocalContractConfig(
                agent_http_base=args.agent_http,
                agent_admin_base=args.agent_admin,
                admin_gateway_base=args.admin_gateway,
                include_agent_http=not args.no_agent,
                include_agent_admin=not args.no_agent,
                include_admin_gateway=not args.no_admin_gateway,
                include_memory=not args.no_memory,
                require_memory_readback=not args.no_memory_readback,
                memory_space_id=args.memory_space_id or None,
                memory_owner_id=args.memory_owner_id or None,
                memory_companion_id=args.memory_companion_id or None,
                memory_isolation_companion_ids=tuple(args.memory_isolation_companion_id),
                memory_recall_samples=max(0, args.memory_recall_samples),
                memory_recall_p95_budget_ms=args.memory_recall_p95_budget_ms,
                require_memory_cleanup=not args.no_memory_cleanup,
                timeout_s=args.timeout_s,
                memory_route_timeout_s=args.memory_route_timeout_s,
                memory_readback_timeout_s=args.memory_readback_timeout_s,
                run_product_acceptance=args.product_acceptance,
                product_acceptance_work_dir=args.product_acceptance_work_dir,
                product_acceptance_sqlite_path=args.sqlite_path,
            )
        )
    )
    print(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2, default=str))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
