"""Standalone conductor entrypoint (prod): `python -m podium.conductor`. One per shard."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from podium.conductor._host import build_conductor
from podium.logging import configure_logging
from podium.settings import get_settings


async def _run() -> None:
    settings = get_settings()
    configure_logging(json=settings.log_json)
    conductor, aclose = build_conductor(settings)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not on all platforms
            loop.add_signal_handler(sig, stop.set)

    try:
        await conductor.run_forever(stop)
    finally:
        await aclose()


if __name__ == "__main__":
    asyncio.run(_run())
