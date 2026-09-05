"""The clip routes only accept ids they could have issued.

A clip id is a 24-character hex digest. The download and thumbnail handlers
turn it straight into a filename under the cache directory, and aiohttp decodes
a %2F inside a match_info value, so an id carrying path separators read files
outside that directory. These tests route real requests through the handlers
the app registers, against a real ClipCache, and pin the guard end to end.
Run from the repo root:

    python tests/test_clip_route_guard.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "proxy"))

from blink_proxy.clip_cache import ClipCache  # noqa: E402
from blink_proxy.routes import clip_download_handler, clip_thumbnail_handler  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


async def _run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        cache_dir = root / "clips"
        cache_dir.mkdir()
        # No proxy token and no Blink client: a cached id is served from disk,
        # and anything that passes the guard but is not cached is a 503.
        app = web.Application()
        app["clip_cache"] = ClipCache(cache_dir, 100 * 1024 * 1024)
        app["clip_index"] = {}
        app["config"] = {}
        app.router.add_get("/clips/{clip_id}.mp4", clip_download_handler)
        app.router.add_get("/clips/{clip_id}.jpg", clip_thumbnail_handler)

        valid = "6de388ac1bf157106b0f7f4a"
        (cache_dir / f"{valid}.mp4").write_bytes(b"MP4-BYTES")
        (cache_dir / f"{valid}.jpg").write_bytes(b"JPEG-BYTES")
        # The file an id with separators in it could reach.
        (root / "secret.mp4").write_bytes(b"NOT-A-CLIP")
        (root / "secret.jpg").write_bytes(b"NOT-A-THUMBNAIL")

        async with TestClient(TestServer(app)) as client:
            resp = await client.get(f"/clips/{valid}.mp4")
            check(
                resp.status == 200 and await resp.read() == b"MP4-BYTES",
                "a cached clip is served by its id",
            )
            resp = await client.get(f"/clips/{valid}.jpg")
            check(
                resp.status == 200 and await resp.read() == b"JPEG-BYTES",
                "a cached thumbnail is served by its id",
            )
            resp = await client.get(f"/clips/{'0' * 24}.mp4")
            check(resp.status == 503, "a well-formed id that is not cached goes to Blink")

            # %2F is the attack: it decodes to a path separator inside clip_id.
            for suffix in ("mp4", "jpg"):
                resp = await client.get(f"/clips/..%2Fsecret.{suffix}")
                body = await resp.read()
                check(
                    resp.status == 404 and b"NOT-A-" not in body,
                    f"an id that climbs out of the cache dir gets no {suffix}",
                )
            for payload, label in [
                ("%2Fetc%2Fpasswd", "an absolute-path id"),
                ("deadbeef", "a short id"),
                ("6de388ac1bf157106b0f7f4a0000", "an over-long id"),
                ("6de388AC1bf157106b0f7f4a", "an upper-case id"),
            ]:
                resp = await client.get(f"/clips/{payload}.mp4")
                check(resp.status == 404, f"{label} is 404, not a lookup")


def main() -> int:
    asyncio.run(_run())
    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\nfailed:")
        for failure in FAILURES:
            print(f"  {failure}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
