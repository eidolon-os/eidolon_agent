"""Isolated production HTTP apps on loopback sockets for cross-project journeys.

Controller credential sourcing and service discovery are test doubles; the
recording model is deterministic unless a real provider is explicitly attached.
Public sessions, Admin adapters, Data SQLite/transactions, Agent runtime reads,
and all HTTP serialization use production implementations. No user database.
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from fastapi import FastAPI

from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "persona-isolated-test-authority-token"


@asynccontextmanager
async def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, lifespan="on", ws="none")
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("HTTP server stopped before startup")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        finally:
            sock.close()


class RecordingModel:
    def __init__(self):
        self.calls = []
        self.delegate = None
        self.fail = False

    async def stream(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.fail:
            raise RuntimeError("injected model outage")
        if self.delegate is not None:
            async for delta in self.delegate.stream(messages, **kwargs):
                yield delta
        else:
            yield LLMDelta(text_delta="收到，我在。", finish=LLMFinishReason.STOP)


@asynccontextmanager
async def persona_stack(tmp_path, monkeypatch):
    from eidolon_admin_server.app.control_plane.clients import (
        AgentActivityClient,
        DataAuthorityClient,
        DataWorkspaceAuthorityClient,
    )
    from eidolon_admin_server.app.control_plane.failure_handler import (
        install_authority_failure_handler,
    )
    from eidolon_admin_server.app.management.router import router as internal_router
    from eidolon_admin_server.bootstrap.config import BootstrapMode, BootstrapSettings
    from eidolon_admin_server.local_api.app import create_app as local_app
    from eidolon_admin_server.local_api.config import LocalApiSettings
    from eidolon_data import DataSettings, DataStore
    from eidolon_data.api.companion_authority import create_app as companion_app
    from eidolon_data.api.workspace_authority import create_app as workspace_app
    from eidolon_sdk.biz.system_data import SystemDataRuntimeClient

    from eidolon_agent.app.admin.authority import SERVICE_TOKEN_ENV
    from eidolon_agent.app.admin.routers.persona_preview import router as preview_router
    from eidolon_agent.domain.personas import PersonasService
    from eidolon_agent.infra.persistence import RuntimeAuthorityPersonaGenomeStore
    from eidolon_agent.infra.system_data import SystemDataCompanionRuntimeAuthority

    # Reuse the existing external Controller test seam; do not replace Local API auth.
    helper = ROOT / "eidolon_admin/server/tests/controller_session_support.py"
    spec = importlib.util.spec_from_file_location("persona_controller_fixture", helper)
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    support.stub_controller_session(
        monkeypatch,
        {
            "contract_version": "1",
            "controller_id": "ectrl-0123456789abcdefabcd",
            "role": "host_admin",
            "reset_epoch": 0,
            "owner_id": "owner-journey",
        },
    )
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    settings = DataSettings(
        sqlite_path=str(tmp_path / "system.sqlite3"),
        object_store_path=str(tmp_path / "objects"),
        audit_nats_url=None,
    )
    store = DataStore.open(settings)
    await store.init_schema()
    await store.owner_commands.create_owner(owner_id="owner-journey")
    await store.owner_commands.create_owner(owner_id="other-owner")
    model = RecordingModel()
    async with AsyncExitStack() as stack:
        stack.push_async_callback(store.close)
        data_url = await stack.enter_async_context(
            serve(companion_app(settings, service_token=TOKEN, memory_roster_token=TOKEN))
        )
        workspace_url = await stack.enter_async_context(
            serve(workspace_app(settings, service_token=TOKEN))
        )
        http = await stack.enter_async_context(httpx.AsyncClient(timeout=45, trust_env=False))

        class Directory:
            async def resolve(self, *, service_id, **kwargs):
                return SimpleNamespace(
                    address=workspace_url if service_id == "data-workspace" else data_url
                )

        runtime = SystemDataCompanionRuntimeAuthority(
            SystemDataRuntimeClient(http, data_url, service_token=TOKEN)
        )
        personas = PersonasService(store=RuntimeAuthorityPersonaGenomeStore(runtime))
        agent = FastAPI()
        agent.state.personas_service = personas
        agent.state.llm_router = model
        agent.include_router(preview_router, prefix="/api/admin")
        agent_url = await stack.enter_async_context(serve(agent))
        internal = FastAPI()
        internal.state.settings = SimpleNamespace(local_api_service_token=TOKEN)
        internal.state.control_plane = SimpleNamespace(
            data=DataAuthorityClient(
                directory=Directory(), client=http, service_token=TOKEN, timeout_seconds=5
            ),
            workspace=DataWorkspaceAuthorityClient(
                directory=Directory(), client=http, service_token=TOKEN, timeout_seconds=5
            ),
            activity=AgentActivityClient(
                base_url=agent_url, client=http, service_token=TOKEN, timeout_seconds=5
            ),
        )
        install_authority_failure_handler(internal)
        internal.include_router(internal_router, prefix="/api")
        internal_url = await stack.enter_async_context(serve(internal))
        public = local_app(
            LocalApiSettings(
                bootstrap=BootstrapSettings(
                    mode=BootstrapMode.DEVELOPMENT,
                    state_dir=tmp_path / "state",
                    runtime_dir=tmp_path / "run",
                    control_socket=tmp_path / "run/control.sock",
                    ble_service_uuid="179e2e95-b1ee-5aa5-8dcf-7519b6c7ac52",
                ),
                admin_base_url=internal_url,
                admin_service_token=TOKEN,
            )
        )
        public_url = await stack.enter_async_context(serve(public))
        auth = await http.post(
            public_url + "/api/local/v1/auth/sessions",
            json={
                "contract_version": "1",
                "purpose": "eidolon-controller-local-auth-v1",
                "controller_id": "ectrl-0123456789abcdefabcd",
                "reset_epoch": 0,
                "challenge": "0123456789abcdefghijklmnopqrstuvwxyzABCDEFG",
                "signature": "abcdefgh",
            },
        )
        assert auth.status_code == 200, auth.text
        headers = {"Authorization": f"Bearer {auth.json()['access_token']}"}
        yield SimpleNamespace(
            http=http,
            base=public_url + "/api/management/v1",
            public_url=public_url,
            headers=headers,
            store=store,
            model=model,
            runtime=runtime,
            personas=personas,
            internal_url=internal_url,
            data_url=data_url,
            agent_url=agent_url,
        )
