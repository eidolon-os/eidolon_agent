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
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote
from uuid import uuid4

import httpx

from eidolon_agent.config import load_settings
from eidolon_agent.core.errors import MemoryUnavailableError, NatsUnavailableError
from eidolon_agent.infra.events import NatsEventBus
from eidolon_agent.infra.memory import (
    McpClientPool,
    MemoryNatsPublisher,
    build_initial_memory_routes,
)

CheckStatus = Literal["passed", "failed", "skipped"]
DependencyUnavailableStatus = Literal["failed", "skipped"]
DEFAULT_MEMORY_SUPERVISOR_RECONCILE_TIMEOUT_S = 120.0


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
class ContractWorkspace:
    owner_id: str
    companion_id: str
    memory_realm_id: str
    genome_id: str | None = None


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
    timeout_s: float = 5.0
    memory_route_timeout_s: float = 0.0
    memory_readback_timeout_s: float = 30.0
    memory_readback_poll_s: float = 0.5
    provision_contract_owner: bool = False
    cleanup_contract_owner: bool = True
    reconcile_memory_supervisor: bool = True
    memory_supervisor_reconcile_timeout_s: float = DEFAULT_MEMORY_SUPERVISOR_RECONCILE_TIMEOUT_S
    contract_owner_id: str = field(default_factory=lambda: f"owner_live_contract_{uuid4().hex[:8]}")
    contract_companion_id: str | None = None
    run_product_acceptance: bool = False
    product_acceptance_work_dir: Path = Path("/tmp/eidolon-product-acceptance")
    product_acceptance_sqlite_path: Path | None = None

    def __post_init__(self) -> None:
        if self.dependency_unavailable_status not in {"failed", "skipped"}:
            raise ValueError("dependency_unavailable_status must be 'failed' or 'skipped'")
        if self.provision_contract_owner and self.memory_route_timeout_s <= 0:
            self.memory_route_timeout_s = 60.0


async def run_live_local_contract(
    config: LiveLocalContractConfig | None = None,
) -> LiveLocalContractReport:
    cfg = config or LiveLocalContractConfig()
    started = time.perf_counter()
    checks: list[ContractCheck] = []
    workspace: ContractWorkspace | None = None

    try:
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

        if cfg.provision_contract_owner:
            provision_check, workspace = await _provision_contract_workspace(cfg)
            checks.append(provision_check)
            if workspace is None:
                return _build_report(cfg=cfg, checks=checks, started=started)
            if cfg.include_memory and cfg.reconcile_memory_supervisor:
                checks.append(await _memory_supervisor_reconcile_check(cfg, workspace))

        if cfg.include_memory:
            selected_memory_space_id = (
                cfg.memory_space_id
                or (workspace.memory_realm_id if workspace is not None else None)
            )
            checks.extend(
                await _memory_contract_checks(
                    cfg,
                    selected_memory_space_id=selected_memory_space_id,
                )
            )

        if cfg.run_product_acceptance:
            checks.append(await _product_acceptance_check(cfg))
    finally:
        if workspace is not None and cfg.cleanup_contract_owner:
            checks.append(await _cleanup_contract_workspace(cfg, workspace))

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
        generated_at=datetime.now(timezone.utc).isoformat(),
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
                not selected_memory_space_id
                or selected_memory_space_id in memory_space_ids
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
        session, tool_names = await _probe_memory_tools(
            pool=pool,
            memory_space_id=memory_space_id,
            timeout_s=cfg.timeout_s,
            unavailable_status=cfg.dependency_unavailable_status,
        )
        checks.append(tool_names)
        if session is None or tool_names.status != "passed":
            return checks
        advertised = set(tool_names.details.get("tool_names") or [])

        checks.append(await _memory_status_check(session, memory_space_id, cfg))
        publish_check, turn_id = await _memory_publish_check(
            bus=bus,
            routes=routes,
            memory_space_id=memory_space_id,
            cfg=cfg,
        )
        checks.append(publish_check)
        if publish_check.status == "passed":
            checks.append(
                await _memory_readback_check(
                    pool=pool,
                    tool_names=advertised,
                    memory_space_id=memory_space_id,
                    turn_id=turn_id,
                    cfg=cfg,
                )
            )
        return checks
    finally:
        await pool.close_all()
        await bus.close()


async def _provision_contract_workspace(
    cfg: LiveLocalContractConfig,
) -> tuple[ContractCheck, ContractWorkspace | None]:
    started = time.perf_counter()
    if not cfg.admin_gateway_base:
        return _check(
            name="contract_owner_provision",
            status="failed",
            required=True,
            started=started,
            summary="admin_gateway_base is required for contract owner provisioning",
        ), None

    owner_id = cfg.contract_owner_id.strip()
    companion_id = (
        cfg.contract_companion_id or f"c_{owner_id.removeprefix('owner_')}"
    ).strip()
    payload = {
        "owner_id": owner_id,
        "owner_display_name": "Live Contract Owner",
        "companion_id": companion_id,
        "companion_display_name": "Live Contract Companion",
        "character_portrait": "A local contract companion used for live E2E validation.",
        "relationship_narrative": "Temporary owner/companion workspace for public contract checks.",
        "voice_portrait": "Brief, concrete, and deterministic.",
        "values": ["contract stability", "clear boundaries"],
        "boundaries": ["Never persist beyond the contract cleanup window."],
    }
    url = _join_url(cfg.admin_gateway_base, "/api/onboarding/initialize")
    try:
        async with httpx.AsyncClient(
            timeout=max(cfg.timeout_s, 15.0),
            follow_redirects=True,
            trust_env=False,
        ) as client:
            response = await client.post(url, json=payload)
    except Exception as exc:
        return _check(
            name="contract_owner_provision",
            status=cfg.dependency_unavailable_status,
            required=True,
            started=started,
            summary=f"{type(exc).__name__}: {exc}",
            details={"url": url, "owner_id": owner_id, "companion_id": companion_id},
        ), None

    details: dict[str, Any] = {
        "url": url,
        "status_code": response.status_code,
        "owner_id": owner_id,
        "companion_id": companion_id,
    }
    if response.status_code >= 400:
        details["body_preview"] = response.text[:500]
        return _check(
            name="contract_owner_provision",
            status="failed",
            required=True,
            started=started,
            summary=f"expected HTTP <400, got {response.status_code}",
            details=details,
        ), None
    try:
        body = response.json()
    except ValueError as exc:
        return _check(
            name="contract_owner_provision",
            status="failed",
            required=True,
            started=started,
            summary=f"response is not JSON: {exc}",
            details=details,
        ), None

    state = body.get("state") if isinstance(body, dict) else None
    master = state.get("master_companion") if isinstance(state, dict) else None
    if not isinstance(master, dict):
        return _check(
            name="contract_owner_provision",
            status="failed",
            required=True,
            started=started,
            summary="onboarding response did not include master_companion",
            details=details,
        ), None
    memory_realm_id = str(master.get("default_memory_realm_id") or "").strip()
    genome_id = str(master.get("current_genome_id") or "").strip() or None
    if not memory_realm_id:
        return _check(
            name="contract_owner_provision",
            status="failed",
            required=True,
            started=started,
            summary="onboarding response did not pin a memory realm",
            details=details,
        ), None

    workspace = ContractWorkspace(
        owner_id=owner_id,
        companion_id=companion_id,
        memory_realm_id=memory_realm_id,
        genome_id=genome_id,
    )
    return _check(
        name="contract_owner_provision",
        status="passed",
        required=True,
        started=started,
        summary="ok",
        details={
            **details,
            "memory_realm_id": memory_realm_id,
            "genome_id": genome_id,
        },
    ), workspace


async def _memory_supervisor_reconcile_check(
    cfg: LiveLocalContractConfig,
    workspace: ContractWorkspace,
) -> ContractCheck:
    started = time.perf_counter()
    if not cfg.admin_gateway_base:
        return _check(
            name="memory_supervisor_reconcile",
            status="failed",
            required=True,
            started=started,
            summary="admin_gateway_base is required for memory supervisor reconcile",
            details={"memory_realm_id": workspace.memory_realm_id},
        )
    url = _join_url(cfg.admin_gateway_base, "/api/memory/supervisor/reconcile")
    try:
        async with httpx.AsyncClient(
            timeout=max(cfg.timeout_s, cfg.memory_supervisor_reconcile_timeout_s),
            follow_redirects=True,
            trust_env=False,
        ) as client:
            response = await client.post(url)
    except Exception as exc:
        return _check(
            name="memory_supervisor_reconcile",
            status=cfg.dependency_unavailable_status,
            required=True,
            started=started,
            summary=f"{type(exc).__name__}: {exc}",
            details={"url": url, "memory_realm_id": workspace.memory_realm_id},
        )

    details: dict[str, Any] = {
        "url": url,
        "status_code": response.status_code,
        "memory_realm_id": workspace.memory_realm_id,
    }
    if response.status_code >= 400:
        details["body_preview"] = response.text[:500]
        return _check(
            name="memory_supervisor_reconcile",
            status="failed",
            required=True,
            started=started,
            summary=f"expected HTTP <400, got {response.status_code}",
            details=details,
        )
    try:
        body = response.json()
    except ValueError as exc:
        return _check(
            name="memory_supervisor_reconcile",
            status="failed",
            required=True,
            started=started,
            summary=f"response is not JSON: {exc}",
            details=details,
        )
    if body.get("ok") is not True:
        return _check(
            name="memory_supervisor_reconcile",
            status="failed",
            required=True,
            started=started,
            summary="memory supervisor reconcile did not return ok=true",
            details={**details, "body": body},
        )
    return _check(
        name="memory_supervisor_reconcile",
        status="passed",
        required=True,
        started=started,
        summary="ok",
        details=details,
    )


async def _cleanup_contract_workspace(
    cfg: LiveLocalContractConfig,
    workspace: ContractWorkspace,
) -> ContractCheck:
    started = time.perf_counter()
    if not cfg.admin_gateway_base:
        return _check(
            name="contract_owner_cleanup",
            status="failed",
            required=True,
            started=started,
            summary="admin_gateway_base is required for contract owner cleanup",
            details={"owner_id": workspace.owner_id},
        )
    url = _join_url(cfg.admin_gateway_base, f"/api/owners/{workspace.owner_id}")
    orphan_url = _join_url(
        cfg.admin_gateway_base,
        f"/api/memory/realms/{quote(workspace.memory_realm_id, safe='')}/orphan",
    )
    try:
        async with httpx.AsyncClient(
            timeout=max(cfg.timeout_s, 30.0),
            follow_redirects=True,
            trust_env=False,
        ) as client:
            response = await client.delete(
                url,
                params={"confirm_owner_id": workspace.owner_id, "purge_memory": "true"},
            )
            orphan_response = None
            if response.status_code < 400 or response.status_code == 404:
                orphan_response = await client.delete(
                    orphan_url,
                    params={"purge_palace": "true"},
                )
    except Exception as exc:
        return _check(
            name="contract_owner_cleanup",
            status="failed",
            required=True,
            started=started,
            summary=f"{type(exc).__name__}: {exc}",
            details={
                "url": url,
                "owner_id": workspace.owner_id,
                "memory_realm_id": workspace.memory_realm_id,
            },
        )
    details: dict[str, Any] = {
        "url": url,
        "status_code": response.status_code,
        "owner_id": workspace.owner_id,
        "memory_realm_id": workspace.memory_realm_id,
    }
    if orphan_response is not None:
        details["memory_orphan_cleanup"] = {
            "url": orphan_url,
            "status_code": orphan_response.status_code,
        }
    if response.status_code == 404:
        if orphan_response is not None and orphan_response.status_code >= 400:
            details["memory_orphan_cleanup"]["body_preview"] = orphan_response.text[:500]
            return _check(
                name="contract_owner_cleanup",
                status="failed",
                required=True,
                started=started,
                summary=(
                    "owner already absent but memory orphan cleanup failed "
                    f"with HTTP {orphan_response.status_code}"
                ),
                details=details,
            )
        return _check(
            name="contract_owner_cleanup",
            status="passed",
            required=True,
            started=started,
            summary="owner already absent",
            details=details,
        )
    if response.status_code >= 400:
        details["body_preview"] = response.text[:500]
        return _check(
            name="contract_owner_cleanup",
            status="failed",
            required=True,
            started=started,
            summary=f"expected HTTP <400, got {response.status_code}",
            details=details,
        )
    if orphan_response is None:
        return _check(
            name="contract_owner_cleanup",
            status="failed",
            required=True,
            started=started,
            summary="memory orphan cleanup was not attempted",
            details=details,
        )
    if orphan_response.status_code >= 400:
        details["memory_orphan_cleanup"]["body_preview"] = orphan_response.text[:500]
        return _check(
            name="contract_owner_cleanup",
            status="failed",
            required=True,
            started=started,
            summary=f"memory orphan cleanup failed with HTTP {orphan_response.status_code}",
            details=details,
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    try:
        orphan_body = orphan_response.json()
    except ValueError:
        orphan_body = {}
    counts = body.get("counts") if isinstance(body, dict) else None
    return _check(
        name="contract_owner_cleanup",
        status="passed",
        required=True,
        started=started,
        summary="ok",
        details={**details, "counts": counts, "memory_orphan_cleanup_body": orphan_body},
    )


async def _probe_memory_tools(
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
            name="memory_mcp_tools",
            status=unavailable_status,
            required=True,
            started=started,
            summary=str(exc),
            details={"memory_space_id": memory_space_id},
        )
    except Exception as exc:
        return None, _check(
            name="memory_mcp_tools",
            status=unavailable_status,
            required=True,
            started=started,
            summary=f"{type(exc).__name__}: {exc}",
            details={"memory_space_id": memory_space_id},
        )
    if tool_names is None:
        return session, _check(
            name="memory_mcp_tools",
            status=unavailable_status,
            required=True,
            started=started,
            summary="MCP list_tools did not return capabilities",
            details={"memory_space_id": memory_space_id},
        )
    advertised = sorted(tool_names)
    missing = {"eidolon_memory_recall_context", "eidolon_memory_status"} - set(tool_names)
    if missing:
        return session, _check(
            name="memory_mcp_tools",
            status="failed",
            required=True,
            started=started,
            summary="required MCP tools are missing",
            details={
                "memory_space_id": memory_space_id,
                "tool_names": advertised,
                "missing": sorted(missing),
            },
        )
    return session, _check(
        name="memory_mcp_tools",
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
    cfg: LiveLocalContractConfig,
) -> tuple[ContractCheck, str]:
    started = time.perf_counter()
    turn_id = f"live-local-contract-{uuid4().hex}"
    publisher = MemoryNatsPublisher(event_bus=bus, routes=routes)
    try:
        await publisher.publish_turn(
            owner_id="live-local-contract",
            companion_id="live-local-contract",
            memory_realm_id=memory_space_id,
            device_id=None,
            session_id="live-local-contract",
            turn_id=turn_id,
            owner_text=_memory_contract_owner_text(turn_id),
            assistant_text="记住了：你偏好把重要的技术验收记录写成短清单。",
            metadata={
                "source": "eidolon-agent-live-local-contract",
                "purpose": "memory-contract-readback",
            },
        )
    except NatsUnavailableError as exc:
        return _check(
            name="memory_nats_publish",
            status=cfg.dependency_unavailable_status,
            required=True,
            started=started,
            summary=str(exc),
            details={"memory_space_id": memory_space_id, "turn_id": turn_id},
        ), turn_id
    except Exception as exc:
        return _check(
            name="memory_nats_publish",
            status="failed",
            required=True,
            started=started,
            summary=f"{type(exc).__name__}: {exc}",
            details={"memory_space_id": memory_space_id, "turn_id": turn_id},
        ), turn_id
    return _check(
        name="memory_nats_publish",
        status="passed",
        required=True,
        started=started,
        summary="ok",
        details={"memory_space_id": memory_space_id, "turn_id": turn_id},
    ), turn_id


def _memory_contract_owner_text(turn_id: str) -> str:
    return (
        "我喜欢把重要的技术验收记录写成短清单，"
        "尤其偏好用 Acquired 半导体播客做灵感来源。"
        f"请记住这个长期偏好，验收标记 {turn_id}。"
    )


async def _memory_readback_check(
    *,
    pool: McpClientPool,
    tool_names: set[str],
    memory_space_id: str,
    turn_id: str,
    cfg: LiveLocalContractConfig,
) -> ContractCheck:
    started = time.perf_counter()
    required = cfg.require_memory_readback
    if "eidolon_memory_get_by_source_turn" not in tool_names:
        return _check(
            name="memory_nats_readback",
            status="failed" if required else "skipped",
            required=required,
            started=started,
            summary="eidolon_memory_get_by_source_turn is not advertised",
            details={"memory_space_id": memory_space_id, "turn_id": turn_id},
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
                "eidolon_memory_get_by_source_turn",
                {"source_turn_id": turn_id, "include_private": True},
                timeout_s=max(0.001, min(cfg.timeout_s, remaining_s)),
            )
        except asyncio.TimeoutError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            last_dependency_unavailable = False
            await _drop_mcp_session(pool, memory_space_id, session)
            await asyncio.sleep(
                min(cfg.memory_readback_poll_s, max(0.0, deadline - time.perf_counter()))
            )
            continue
        except MemoryUnavailableError as exc:
            last_error = str(exc)
            last_dependency_unavailable = True
            await _drop_mcp_session(pool, memory_space_id, session)
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
                details={"memory_space_id": memory_space_id, "turn_id": turn_id},
            )
        last_payload = payload
        last_error = None
        last_dependency_unavailable = False
        record = payload.get("record") if isinstance(payload, dict) else None
        if isinstance(record, dict):
            return _check(
                name="memory_nats_readback",
                status="passed",
                required=required,
                started=started,
                summary="ok",
                details={
                    "memory_space_id": memory_space_id,
                    "turn_id": turn_id,
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
            "turn_id": turn_id,
            "timeout_s": cfg.memory_readback_timeout_s,
            "last_payload": last_payload,
            "last_error": last_error,
        },
    )


async def _call_mcp_tool_with_timeout(
    session: Any,
    name: str,
    arguments: dict[str, Any],
    *,
    timeout_s: float,
) -> dict[str, Any]:
    task = asyncio.create_task(session.call_tool(name, arguments))
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_s)
    except asyncio.CancelledError as exc:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        raise asyncio.TimeoutError(f"MCP call cancelled before completion: {exc}") from exc
    if task in done:
        try:
            return task.result()
        except asyncio.CancelledError as exc:
            raise asyncio.TimeoutError(f"MCP call cancelled before completion: {exc}") from exc

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    raise asyncio.TimeoutError()


async def _drop_mcp_session(
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
    parser.add_argument("--memory-space-id", default=os.getenv("EIDOLON_AGENT_LIVE_MEMORY_SPACE_ID", ""))
    parser.add_argument("--timeout-s", type=float, default=5.0)
    parser.add_argument("--memory-route-timeout-s", type=float, default=0.0)
    parser.add_argument("--memory-readback-timeout-s", type=float, default=30.0)
    parser.add_argument("--no-agent", action="store_true")
    parser.add_argument("--no-admin-gateway", action="store_true")
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--no-memory-readback", action="store_true")
    parser.add_argument("--provision-contract-owner", action="store_true")
    parser.add_argument("--no-memory-supervisor-reconcile", action="store_true")
    parser.add_argument(
        "--memory-supervisor-reconcile-timeout-s",
        type=float,
        default=DEFAULT_MEMORY_SUPERVISOR_RECONCILE_TIMEOUT_S,
    )
    parser.add_argument("--keep-contract-owner", action="store_true")
    parser.add_argument("--contract-owner-id", default="")
    parser.add_argument("--contract-companion-id", default="")
    parser.add_argument("--product-acceptance", action="store_true")
    parser.add_argument("--product-acceptance-work-dir", type=Path, default=Path("/tmp/eidolon-product-acceptance"))
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
                timeout_s=args.timeout_s,
                memory_route_timeout_s=args.memory_route_timeout_s,
                memory_readback_timeout_s=args.memory_readback_timeout_s,
                provision_contract_owner=args.provision_contract_owner,
                cleanup_contract_owner=not args.keep_contract_owner,
                reconcile_memory_supervisor=not args.no_memory_supervisor_reconcile,
                memory_supervisor_reconcile_timeout_s=args.memory_supervisor_reconcile_timeout_s,
                contract_owner_id=args.contract_owner_id or f"owner_live_contract_{uuid4().hex[:8]}",
                contract_companion_id=args.contract_companion_id or None,
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
