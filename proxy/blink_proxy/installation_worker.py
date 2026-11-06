"""Run the freshly installed proxy until its parent exits or its deadline passes."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import shutil
import sys
from pathlib import Path

from aiohttp import web

from .config import load_config
from .routes import make_app


async def main(root: Path) -> None:
    config, base = load_config(root / "config.json")
    runner = web.AppRunner(await make_app(config, base, None), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    # aiohttp exposes the bound sockets through the runner addresses.
    (root / "ready.json").write_text(json.dumps({"port": runner.addresses[0][1]}))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    parent = os.getppid()
    try:
        # Independently bounded: an abrupt parent crash cannot leave a worker
        # holding a Blink session indefinitely.
        for _ in range(30 * 60):
            if os.getppid() != parent:
                break
            try:
                await asyncio.wait_for(stop.wait(), 1)
                break
            except TimeoutError:
                pass
    finally:
        await runner.cleanup()
        if os.getppid() != parent:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
