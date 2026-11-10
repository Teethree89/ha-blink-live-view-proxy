"""Run inside either proxy image; no Blink account or Supervisor is contacted."""

import asyncio
from pathlib import Path
import sys

SOURCE = Path("/opt/proxy")
if not SOURCE.exists():
    SOURCE = Path(__file__).resolve().parents[1] / "proxy"
sys.path.insert(0, str(SOURCE))

from blink_proxy.installation_test import InstallationTest  # noqa: E402


async def main():
    test = InstallationTest({}, source=SOURCE, ttl=300)
    root = None
    process = None
    try:
        await test.start(False)
        root = test.root
        async with asyncio.timeout(240):
            while test.phase not in {"ready", "failed"}:
                await asyncio.sleep(0.2)
        assert test.phase == "ready", test.status()
        assert (await test.request("GET", "health"))["ok"]
        auth = await test.request("GET", "auth/status")
        assert auth["state"] == "idle" and not auth["authenticated"]
        assert not (root / "state/blink-auth.json").exists()
        process = test._process
    finally:
        await test.discard()
    assert root is not None and not root.exists()
    assert process is not None and process.returncode is not None
    assert test._token == ""
    import shutil
    assert shutil.which("ffmpeg")
    print("PASS: fresh venv/dependencies, health, generated token, no Blink login, process/data cleanup")


asyncio.run(main())
