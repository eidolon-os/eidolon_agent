"""In-process Runtime token -> System Data HTTP -> Agent authority contract."""

from __future__ import annotations

import httpx
import pytest
from eidolon_data import DataSettings, DataStore
from eidolon_data.api.companion_authority import create_app
from eidolon_sdk.biz.runtime import RuntimeTokenVerifier, sign_runtime_token
from eidolon_sdk.biz.system_data import SystemDataRuntimeClient

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.domain.runtime_session import RuntimeSessionAuthorizer
from eidolon_agent.infra.system_data import SystemDataCompanionRuntimeAuthority

pytestmark = pytest.mark.integration

_OWNER_ID = "owner-session-integration"
_COMPANION_ID = "companion-session-integration"
_SESSION_ID = "livekit-room-session-integration"
_DATA_TOKEN = "data-authority-session-integration-token"
_RUNTIME_SECRET = "runtime-session-integration-secret"


async def _seed_system_data(tmp_path) -> DataSettings:
    settings = DataSettings(
        sqlite_path=str(tmp_path / "eidolon-system.sqlite3"),
        object_store_path=str(tmp_path / "objects"),
    )
    store = DataStore.open(settings)
    try:
        await store.init_schema()
        await store.owner_commands.create_owner(owner_id=_OWNER_ID)
        await store.companion_workspaces.provision_workspace(
            owner_id=_OWNER_ID,
            companion_id=_COMPANION_ID,
            genome_id="genome-session-integration",
            realm_id="realm-session-integration",
        )
    finally:
        await store.close()
    return settings


async def _verified_identity(*, owner_id: str):
    token, _ = sign_runtime_token(
        secret=_RUNTIME_SECRET,
        owner_id=owner_id,
        companion_id=_COMPANION_ID,
        session_id=_SESSION_ID,
        ttl_seconds=60,
    )
    return await RuntimeTokenVerifier(secret=_RUNTIME_SECRET).verify(token)


async def test_verified_session_resolves_real_system_data_authority(tmp_path) -> None:
    settings = await _seed_system_data(tmp_path)
    app = create_app(settings, service_token=_DATA_TOKEN)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://system-data.test",
        ) as http_client,
    ):
        identity = await _verified_identity(owner_id=_OWNER_ID)
        authority = SystemDataCompanionRuntimeAuthority(
            SystemDataRuntimeClient(
                http_client,
                "http://system-data.test",
                service_token=_DATA_TOKEN,
            )
        )

        scope = await RuntimeSessionAuthorizer(authority).authorize(
            owner_id=identity.owner_id,
            companion_id=identity.companion_id,
            device_id=identity.device_id,
            session_id=identity.session_id,
        )

    assert scope.owner_id == _OWNER_ID
    assert scope.companion_id == _COMPANION_ID
    assert scope.session_id == _SESSION_ID
    assert scope.device_id is None
    assert scope.runtime.memory_realm_id == "realm-session-integration"
    assert scope.runtime.genome_id == "genome-session-integration"


async def test_cross_owner_token_cannot_authorize_existing_companion(tmp_path) -> None:
    settings = await _seed_system_data(tmp_path)
    app = create_app(settings, service_token=_DATA_TOKEN)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://system-data.test",
        ) as http_client,
    ):
        identity = await _verified_identity(owner_id="another-owner")
        authority = SystemDataCompanionRuntimeAuthority(
            SystemDataRuntimeClient(
                http_client,
                "http://system-data.test",
                service_token=_DATA_TOKEN,
            )
        )

        with pytest.raises(NotFoundError, match="companion not found for owner"):
            await RuntimeSessionAuthorizer(authority).authorize(
                owner_id=identity.owner_id,
                companion_id=identity.companion_id,
                device_id=identity.device_id,
                session_id=identity.session_id,
            )
