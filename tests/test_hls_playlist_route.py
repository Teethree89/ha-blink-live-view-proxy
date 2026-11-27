"""The add-on's HLS playlist route: what it answers when the stream is not there.

A camera too slow to start, or ffmpeg dying, used to escape as an unhandled
exception and come back as a bare 500 ("Server got itself in trouble"), which
read as the proxy crashing. Seen on a driveway camera's first live view after
the add-on restarted. And a playlist must go out as exactly
application/vnd.apple.mpegurl: with "; charset=utf-8" appended, Apple's
mediastreamvalidator rejects the type. Run from the repo root:

    python tests/test_hls_playlist_route.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import types

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "proxy"))

from blink_proxy.routes import hls_playlist_handler  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str, detail: object = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILURES.append(label)


class FakeSession:
    def __init__(self, playlist: pathlib.Path, outcome: BaseException | None) -> None:
        self.playlist = playlist
        self.outcome = outcome

    async def wait_ready(self) -> None:
        if self.outcome is not None:
            raise self.outcome

    def register_liveview_key(self, _key: str) -> None:
        pass


class FakeManager:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def get_or_start(self, _slug: str) -> FakeSession:
        return self.session


async def fetch(outcome: BaseException | None) -> tuple[int, str, str]:
    with tempfile.TemporaryDirectory() as raw:
        playlist = pathlib.Path(raw) / "index.m3u8"
        playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\nsegment_00000.ts\n")
        app = web.Application()
        app["hls_manager"] = FakeManager(FakeSession(playlist, outcome))
        app["client"] = types.SimpleNamespace(ready=True)
        app.router.add_get("/cameras/{slug}/hls/index.m3u8", hls_playlist_handler)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/cameras/driveway/hls/index.m3u8?session=abc")
            return response.status, response.headers.get("Content-Type", ""), await response.text()


async def main() -> int:
    print("add-on HLS playlist route")
    status, kind, body = await fetch(None)
    check(status == 200 and "segment_00000.ts" in body, "a ready stream is served")
    check(kind == "application/vnd.apple.mpegurl",
          "as exactly application/vnd.apple.mpegurl, no charset (Apple 451001)", kind)

    status, _kind, body = await fetch(TimeoutError("HLS playlist not ready after 30s"))
    check(status == 504, "a camera too slow to start is a 504, not a bare 500", status)
    check("did not start sending video in time" in body and "30s" in body,
          "and says why", body.strip())

    status, _kind, body = await fetch(RuntimeError("ffmpeg exited with 1"))
    check(status == 502 and "ffmpeg exited with 1" in body,
          "ffmpeg failing is a 502 that names it", f"{status} {body.strip()}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failed: {', '.join(FAILURES)}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
