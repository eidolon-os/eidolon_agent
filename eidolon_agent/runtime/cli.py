"""``eidolon-agent`` console entrypoint.

Runs the bootstrap, starts gRPC + HTTP + Admin servers, blocks until SIGTERM/SIGINT.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn

from eidolon_agent.config import load_settings
from eidolon_agent.runtime.bootstrap import build_application
from eidolon_agent.runtime.lifecycle import install_shutdown_handlers

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eidolon-agent")
    parser.add_argument("--http-only", action="store_true", help="serve HTTP only (skip gRPC)")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


async def _run(args) -> int:  # type: ignore[no-untyped-def]
    settings = load_settings()
    container = await build_application(settings=settings)

    stop = asyncio.Event()
    install_shutdown_handlers(stop)

    grpc_task = None
    if not args.http_only:
        await container.grpc_server.start()
        grpc_task = asyncio.create_task(container.grpc_server.wait_for_termination(), name="grpc-server")

    http_server = uvicorn.Server(uvicorn.Config(
        container.http_app,
        host=settings.http.host,
        port=settings.http.port,
        log_config=None,
        lifespan="off",
    ))
    admin_server = uvicorn.Server(uvicorn.Config(
        container.admin_app,
        host=settings.http.host,
        port=settings.http.admin_port,
        log_config=None,
        lifespan="off",
    ))

    http_task = asyncio.create_task(http_server.serve(), name="http-server")
    admin_task = asyncio.create_task(admin_server.serve(), name="admin-server")

    _log.info(
        "eidolon-agent ready (gRPC %s:%d / HTTP %s:%d / Admin %s:%d)",
        settings.grpc.tcp_host, settings.grpc.tcp_port,
        settings.http.host, settings.http.port,
        settings.http.host, settings.http.admin_port,
    )

    await stop.wait()
    _log.info("shutdown requested; draining")

    http_server.should_exit = True
    admin_server.should_exit = True
    if grpc_task is not None:
        await container.grpc_server.stop(grace_s=settings.runtime.drain_timeout_s)
    for task in (http_task, admin_task):
        try:
            await asyncio.wait_for(task, timeout=settings.runtime.drain_timeout_s)
        except asyncio.TimeoutError:
            _log.warning("server failed to drain in time")
    if grpc_task is not None:
        grpc_task.cancel()
    return 0


if __name__ == "__main__":
    sys.exit(main())
