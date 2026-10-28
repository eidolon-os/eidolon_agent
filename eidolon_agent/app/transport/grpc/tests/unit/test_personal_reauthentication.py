from types import SimpleNamespace

import pytest
from eidolon_sdk.biz.runtime import (
    RuntimeTokenRevokedError,
    RuntimeTokenVerifier,
    RuntimeUnauthenticatedError,
    session_revocation_keys,
    sign_runtime_token,
)

from eidolon_agent.app.transport.grpc import interceptors


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["personal", "provisional"])
async def test_reauthentication_uses_original_token_and_current_revocation_store(memory_mode):
    revoked = set()

    class Store:
        async def get(self, key):
            return b"revoked" if key in revoked else None

    secret = "test-secret-with-enough-entropy-32b"
    token, _ = sign_runtime_token(
        secret=secret,
        owner_id="owner",
        companion_id="companion",
        session_id="session",
        person_id="person",
        identity_epoch=1,
        person_revision=1,
        ttl_seconds=60,
        source_session_id="session",
        memory_mode=memory_mode,
    )
    interceptor = interceptors.AuthInterceptor(
        RuntimeTokenVerifier(secret=secret, revocation_kv=Store(), allow_provisional=True)
    )
    old_identity = interceptors._current_identity.set(None)
    old_verify = interceptors._current_reauthenticate.set(None)
    try:
        with pytest.raises(RuntimeUnauthenticatedError):
            await interceptors.reauthenticate_current()

        async def continuation(_):
            return "handler"

        assert (
            await interceptor.intercept_service(
                continuation,
                SimpleNamespace(
                    method="/Chat", invocation_metadata=(("authorization", f"Bearer {token}"),)
                ),
            )
            == "handler"
        )
        first = interceptors.current_identity()
        assert first.person_id == "person"
        assert first.memory_mode == memory_mode
        assert await interceptors.reauthenticate_current() == first
        revoked.add(session_revocation_keys("session")[0])
        with pytest.raises(RuntimeTokenRevokedError):
            await interceptors.reauthenticate_current()
    finally:
        interceptors._current_identity.reset(old_identity)
        interceptors._current_reauthenticate.reset(old_verify)
