"""Process lifecycle helpers — signal handling and graceful shutdown."""

from __future__ import annotations

import asyncio
import logging
import signal

_log = logging.getLogger(__name__)


def install_shutdown_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows; rely on KeyboardInterrupt for SIGINT.
            pass
    _log.debug("shutdown signal handlers installed")
