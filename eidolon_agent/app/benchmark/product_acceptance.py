"""Local product acceptance profile for the canonical persona genome flow."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import socket
import sys
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from eidolon_data import DataSettings, DataStore
from fastapi import FastAPI

from eidolon_agent.app.runtime.bootstrap import build_application
from eidolon_agent.config.settings import (
    BodyControlSettings,
    GrpcSettings,
    HttpSettings,
    LLMSettings,
    LongTaskSettings,
    ObservabilitySettings,
    RuntimeSettings,
    Settings,
)
from eidolon_agent.infra.persistence import EidolonDataConversationReader


@dataclass
class ProductAcceptanceResult:
    passed: bool
    mode: str
    owner_id: str
    companion_id: str
    memory_realm_id: str
    genome_id: str
    genome_hash: str
    chat_event_kinds: list[str]
    turn_metadata: dict[str, Any]
    event_types: list[str]
    cleanup_counts: dict[str, Any]
    elapsed_ms: float


class ProductAcceptanceUnavailable(RuntimeError):
    """Raised when optional cross-repo Admin imports are unavailable."""


async def run_product_acceptance_profile(
    *,
    work_dir: Path,
    sqlite_path: Path | None = None,
    owner_id: str = "owner_acceptance",
    companion_id: str = "c_acceptance_companion",
) -> ProductAcceptanceResult:
    """Exercise the Admin onboarding API and Agent admin chat-test gRPC path.

    This is the PR-safe deterministic profile: no Hub, Channel, device, NATS, or
    real memory service. It still uses the formal Admin/Onboarding HTTP API via
    ASGI, the real Agent gRPC Chat servicer, SQLite persistence, and the runtime
    token contract.
    """

    started = time.perf_counter()
    work_dir.mkdir(parents=True, exist_ok=True)
    sqlite_path = sqlite_path or (work_dir / "eidolon.sqlite3")
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    secret = "product-acceptance-local-secret-32-bytes-minimum"
    cleanup_done = False
    old_data_path = os.environ.get("EIDOLON_DATA_SQLITE_PATH")
    old_jwt_secret = os.environ.get("PAIRING_JWT_SECRET")

    container = None
    try:
        provisioned = await _initialize_via_admin_onboarding_api(
            sqlite_path=sqlite_path,
            owner_id=owner_id,
            companion_id=companion_id,
        )
        os.environ["EIDOLON_DATA_SQLITE_PATH"] = str(sqlite_path)
        os.environ["PAIRING_JWT_SECRET"] = secret

        settings = _profile_settings(work_dir)
        container = await build_application(settings=settings)
        await container.grpc_server.start()

        store = container.data_store
        companion = await store.companions.get(companion_id)
        assert companion is not None
        genome = await store.persona_repo.get_genome(companion.current_genome_id)
        assert genome is not None

        chat_events = await _run_admin_chat_test(
            container.admin_app,
            owner_id=owner_id,
            companion_id=companion_id,
            text="请用一句话确认你知道自己的名字和当前 owner。",
        )
        kinds = [str(event.get("kind")) for event in chat_events if event.get("kind")]
        if "DONE" not in kinds:
            raise AssertionError(f"admin chat test did not finish: {kinds}")
        if container.background_tasks is not None and hasattr(container.background_tasks, "drain"):
            await container.background_tasks.drain(timeout_s=5)

        reader = EidolonDataConversationReader(store)
        turns = await reader.list_turns_by_owner(
            owner_id=owner_id,
            companion_id=companion_id,
            limit=5,
        )
        if not turns:
            raise AssertionError("no turn persisted after admin chat test")
        turn = turns[0]
        expected_metadata = {
            "owner_id": owner_id,
            "companion_id": companion_id,
            "memory_realm_id": provisioned["memory_realm_id"],
            "genome_id": genome.genome_id,
            "genome_hash": genome.genome_hash,
        }
        for key, value in expected_metadata.items():
            if turn.get(key) != value:
                raise AssertionError(
                    f"turn metadata mismatch for {key}: {turn.get(key)!r} != {value!r}"
                )

        events = await store.events.list_for_owner(owner_id, limit=100)
        event_types = [event.event_type for event in events]
        required_events = {
            "owner.created",
            "companion.created",
            "persona.genome.committed",
            "memory_realm.created",
            "companion.workspace.initialized",
            "device.web_body.provisioned",
        }
        missing = required_events.difference(event_types)
        if missing:
            raise AssertionError(f"missing event timeline entries: {sorted(missing)}")

        cleanup = await store.dev_maintenance.delete_owner_tree(owner_id)
        if not cleanup.deleted:
            raise AssertionError("cleanup did not delete acceptance owner")
        cleanup_done = True

        return ProductAcceptanceResult(
            passed=True,
            mode="deterministic",
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=provisioned["memory_realm_id"],
            genome_id=genome.genome_id,
            genome_hash=genome.genome_hash,
            chat_event_kinds=kinds,
            turn_metadata={key: turn.get(key) for key in expected_metadata},
            event_types=event_types,
            cleanup_counts=asdict(cleanup),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        )
    finally:
        if not cleanup_done:
            await _cleanup_owner_best_effort(
                sqlite_path=sqlite_path,
                owner_id=owner_id,
                container=container,
            )
        if container is not None:
            await _close_container(container)
        _restore_env("EIDOLON_DATA_SQLITE_PATH", old_data_path)
        _restore_env("PAIRING_JWT_SECRET", old_jwt_secret)


async def _initialize_via_admin_onboarding_api(
    *,
    sqlite_path: Path,
    owner_id: str,
    companion_id: str,
) -> dict[str, str]:
    try:
        onboarding_router = _import_admin_module(
            "eidolon_admin_server.app.onboarding.router"
        ).router
    except ImportError as exc:
        raise ProductAcceptanceUnavailable(
            "eidolon_admin_server is not importable; install/link eidolon_admin to run "
            "the product acceptance profile"
        ) from exc

    store = DataStore.open(DataSettings(sqlite_path=str(sqlite_path)))
    await store.init_schema()
    app = FastAPI()
    app.state.data_store = store
    app.state.memory_supervisor_client = None
    app.include_router(onboarding_router, prefix="/api")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://admin-asgi",
        ) as client:
            response = await client.post(
                "/api/onboarding/initialize",
                json={
                    "owner_id": owner_id,
                    "owner_display_name": "Acceptance Owner",
                    "companion_id": companion_id,
                    "companion_display_name": "Acceptance Companion",
                    "character_portrait": "A calm local product acceptance companion.",
                    "relationship_narrative": "Trusted local E2E acceptance partner.",
                    "voice_portrait": "Brief, grounded, and concrete.",
                    "values": ["clarity", "continuity"],
                    "boundaries": ["Never invent runtime identity."],
                },
            )
            response.raise_for_status()

            state = response.json()["state"]
            if state["master_companion"]["companion_id"] != companion_id:
                raise AssertionError("onboarding did not create the requested companion")
            companion = await store.companions.get(companion_id)
            if companion is None:
                raise AssertionError("onboarding companion missing from database")
            if not companion.current_genome_id or not companion.default_memory_realm_id:
                raise AssertionError("onboarding did not pin genome and memory realm")
            genome = await store.persona_repo.get_genome(companion.current_genome_id)
            if genome is None:
                raise AssertionError("onboarding current genome missing from database")
            return {
                "companion_id": companion.companion_id,
                "memory_realm_id": companion.default_memory_realm_id,
                "genome_id": genome.genome_id,
                "genome_hash": genome.genome_hash,
            }
    finally:
        await store.close()


def _import_admin_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except ImportError:
        monorepo = Path(__file__).resolve().parents[4]
        admin_server = monorepo / "eidolon_admin" / "server"
        inserted = False
        if admin_server.is_dir() and str(admin_server) not in sys.path:
            sys.path.insert(0, str(admin_server))
            inserted = True
        try:
            return importlib.import_module(module_name)
        finally:
            if inserted:
                with suppress(ValueError):
                    sys.path.remove(str(admin_server))


async def _run_admin_chat_test(
    admin_app,
    *,
    owner_id: str,
    companion_id: str,
    text: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app),
        base_url="http://agent-admin-asgi",
        timeout=20.0,
    ) as client:
        async with client.stream(
            "POST",
            "/api/admin/chat/test",
            json={
                "owner_id": owner_id,
                "companion_id": companion_id,
                "text": text,
                "persist_memory": True,
            },
        ) as response:
            response.raise_for_status()
            block: list[str] = []
            async for line in response.aiter_lines():
                if line:
                    block.append(line)
                    continue
                event = _decode_sse_block(block)
                block = []
                if event:
                    data = event.get("data")
                    if isinstance(data, dict):
                        events.append(data)
            event = _decode_sse_block(block)
            if event and isinstance(event.get("data"), dict):
                events.append(event["data"])
    return events


def _decode_sse_block(lines: list[str]) -> dict[str, Any] | None:
    if not lines:
        return None
    event_type = "message"
    data = ""
    for line in lines:
        if line.startswith("event:"):
            event_type = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data = line.removeprefix("data:").strip()
    if not data:
        return {"event": event_type, "data": None}
    return {"event": event_type, "data": json.loads(data)}


def _profile_settings(work_dir: Path) -> Settings:
    grpc_port = _free_port()
    return Settings(
        grpc=GrpcSettings(tcp_host="127.0.0.1", tcp_port=grpc_port),
        http=HttpSettings(
            host="127.0.0.1",
            port=_free_port(),
            admin_port=_free_port(),
            cors_origins=[],
        ),
        llm=LLMSettings(
            models=[],
            default_model="fake",
            fallback_models=[],
            startup_warm_enabled=False,
        ),
        long_task=LongTaskSettings(transport="disabled"),
        body_control=BodyControlSettings(enabled=False),
        observability=ObservabilitySettings(
            log_json=False,
            log_dir=work_dir / "logs",
            metrics_enabled=False,
        ),
        runtime=RuntimeSettings(
            log_dir=work_dir / "logs",
            run_dir=work_dir / "run",
            debug_dir=work_dir / "debug",
            warmup_enabled=False,
            recover_active_instances=False,
            standalone=True,
        ),
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _close_container(container) -> None:  # type: ignore[no-untyped-def]
    with suppress(Exception):
        if container.grpc_server is not None:
            await container.grpc_server.stop(grace_s=0)
    with suppress(Exception):
        refresher = container.extras.get("memory_discovery_refresher")
        if refresher is not None:
            await refresher.stop()
    with suppress(Exception):
        worker = container.extras.get("long_task_worker")
        if worker is not None and hasattr(worker, "stop"):
            await worker.stop()
    with suppress(Exception):
        if container.background_tasks is not None and hasattr(container.background_tasks, "drain"):
            await container.background_tasks.drain(timeout_s=1)
    with suppress(Exception):
        if container.personas_service is not None and hasattr(container.personas_service, "stop"):
            await container.personas_service.stop()
    with suppress(Exception):
        if container.memory_port is not None and hasattr(container.memory_port, "close"):
            await container.memory_port.close()
    with suppress(Exception):
        if container.llm_router is not None and hasattr(container.llm_router, "close"):
            await container.llm_router.close()
    with suppress(Exception):
        if container.data_store is not None and hasattr(container.data_store, "close"):
            await container.data_store.close()


async def _cleanup_owner_best_effort(
    *,
    sqlite_path: Path,
    owner_id: str,
    container,
) -> None:
    with suppress(Exception):
        if container is not None and container.data_store is not None:
            await container.data_store.dev_maintenance.delete_owner_tree(owner_id)
            return
    with suppress(Exception):
        store = DataStore.open(DataSettings(sqlite_path=str(sqlite_path)))
        try:
            await store.dev_maintenance.delete_owner_tree(owner_id)
        finally:
            await store.close()


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="product-acceptance")
    parser.add_argument("--work-dir", type=Path, default=Path("/tmp/eidolon-product-acceptance"))
    parser.add_argument("--sqlite-path", type=Path, default=None)
    args = parser.parse_args(argv)
    result = asyncio.run(
        run_product_acceptance_profile(
            work_dir=args.work_dir,
            sqlite_path=args.sqlite_path,
        )
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
