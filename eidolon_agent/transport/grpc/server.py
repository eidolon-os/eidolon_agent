"""gRPC server — wires interceptors, servicers, TCP + optional UDS listeners."""

from __future__ import annotations

import logging
from pathlib import Path

import grpc
import grpc.aio

from eidolon_agent.transport.grpc.chat_servicer import EidolonAgentServicer
from eidolon_agent.transport.grpc.interceptors import AuthInterceptor
from eidolon_agent.transport.grpc.proto import pbg

_log = logging.getLogger(__name__)


class GrpcServer:
    def __init__(
        self,
        *,
        servicer: EidolonAgentServicer,
        token_verifier,
        tcp_host: str = "127.0.0.1",
        tcp_port: int = 50051,
        uds_path: Path | None = None,
        keepalive_time_s: int = 20,
        keepalive_timeout_s: int = 5,
        max_connection_idle_s: int = 600,
    ) -> None:
        self._servicer = servicer
        self._verifier = token_verifier
        self._tcp_host = tcp_host
        self._tcp_port = tcp_port
        self._uds_path = uds_path
        self._server: grpc.aio.Server | None = None
        self._opts = [
            ("grpc.keepalive_time_ms", keepalive_time_s * 1000),
            ("grpc.keepalive_timeout_ms", keepalive_timeout_s * 1000),
            ("grpc.max_connection_idle_ms", max_connection_idle_s * 1000),
            ("grpc.so_reuseport", 1),
        ]

    async def start(self) -> None:
        self._server = grpc.aio.server(
            interceptors=[AuthInterceptor(self._verifier)],
            options=self._opts,
        )
        pbg.add_EidolonAgentServicer_to_server(self._servicer, self._server)
        tcp_target = f"{self._tcp_host}:{self._tcp_port}"
        self._server.add_insecure_port(tcp_target)
        _log.info("gRPC listening on tcp %s", tcp_target)
        if self._uds_path is not None:
            uds = self._uds_path.expanduser()
            uds.parent.mkdir(parents=True, exist_ok=True)
            if uds.exists():
                uds.unlink()
            self._server.add_insecure_port(f"unix:{uds}")
            _log.info("gRPC also listening on UDS %s", uds)
        await self._server.start()

    async def wait_for_termination(self) -> None:
        if self._server is not None:
            await self._server.wait_for_termination()

    async def stop(self, *, grace_s: float = 30.0) -> None:
        if self._server is not None:
            await self._server.stop(grace_s)
            self._server = None
