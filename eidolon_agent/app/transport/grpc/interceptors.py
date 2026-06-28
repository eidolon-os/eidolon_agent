"""Server-side gRPC interceptors: auth + tracing."""

from __future__ import annotations

import logging

import grpc
from grpc.aio import ServerInterceptor

_log = logging.getLogger(__name__)

_PUBLIC_METHODS = frozenset()


class AuthInterceptor(ServerInterceptor):
    """Bearer-token auth interceptor. Public RPCs are allow-listed by full name."""

    def __init__(self, verifier) -> None:
        self._verifier = verifier

    async def intercept_service(
        self,
        continuation,
        handler_call_details: grpc.HandlerCallDetails,
    ):
        method = handler_call_details.method
        if method in _PUBLIC_METHODS:
            return await continuation(handler_call_details)
        metadata = dict(handler_call_details.invocation_metadata or ())
        token = metadata.get("authorization", "")
        if not token.lower().startswith("bearer "):
            return _unauthenticated("missing bearer token")
        raw = token[7:].strip()
        try:
            identity = await self._verifier.verify(raw)
        except Exception as exc:
            _log.info("auth rejected: %s", exc)
            return _unauthenticated(str(exc))
        # Attach identity as a magic invocation_metadata entry — servicers can
        # retrieve via context.invocation_metadata(). For simplicity we stash
        # in a process-local map keyed by the handler call.
        _AuthScope.current.set(identity)  # type: ignore[attr-defined]
        return await continuation(handler_call_details)


def _unauthenticated(msg: str):
    async def _abort(request, context):  # type: ignore[no-untyped-def]
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, msg)

    return grpc.unary_unary_rpc_method_handler(_abort)


# Lightweight contextvar-based scope so servicers can grab the verified Identity
# without changing every method signature.

import contextvars  # noqa: E402

_current_identity: contextvars.ContextVar = contextvars.ContextVar("identity", default=None)


class _AuthScope:
    current = _current_identity


def current_identity():
    return _current_identity.get()
