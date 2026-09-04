"""Checks on the clip cache and its thumbnails.

No Blink, no network. The cache is exercised with a stand-in ClipManager whose
"download" is a local byte string, and ffmpeg is only run when it is on PATH -
CI's test job does not install it, and the argument list is checked either way.
Run from the repo root:

    python tests/test_clip_cache.py

What is being defended:

  * a clip is fetched from Blink exactly once, however many rows and previews
    ask for it. prepare_download() has the Sync Module upload the clip and
    polls the cloud until it lands, so every avoided fetch is seconds and a
    round of API calls.
  * only one fetch is in flight at a time, even when a page of thumbnails asks
    for twenty at once. Concurrent prepare_download() calls are the pattern
    that gets an account rate-limited.
  * the cache is bounded, oldest first, and never evicts what it is in the
    middle of handing out.
  * a thumbnail is a JPEG of the first frame, and is cut from the cached copy
    rather than fetched again.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "proxy"))

from blink_proxy.clip_cache import (  # noqa: E402
    ClipCache,
    prune_directory,
    thumbnail_args,
)
from blink_proxy.clips import clip_id  # noqa: E402
from blink_proxy.routes import clip_cache_dir  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def clip(name: str = "Front Door", offset: int = 0) -> dict:
    return {
        "source": "local",
        "slug": name.lower().replace(" ", "_"),
        "camera_name": name,
        "created_at": datetime.datetime(2026, 9, 4, 12, 0, offset, tzinfo=datetime.timezone.utc),
        "size": 1234,
        "url": f"https://blink.example/{name}",
    }


class FakeContent:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def iter_chunked(self, size: int):
        for start in range(0, len(self.payload), size):
            await asyncio.sleep(0)
            yield self.payload[start : start + size]


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.content = FakeContent(payload)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeManager:
    """Counts fetches and how many overlap, which is the whole point."""

    def __init__(self, payload: bytes = b"\x00" * 4096, delay: float = 0.02) -> None:
        self.payload = payload
        self.delay = delay
        self.fetches = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def open_clip_response(self, _clip):
        self.fetches += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(self.delay)
        self.in_flight -= 1
        return FakeResponse(self.payload)


def test_fetch_once() -> None:
    print("\na clip is fetched once")
    with tempfile.TemporaryDirectory() as tmp:
        cache = ClipCache(pathlib.Path(tmp), 100 * 1024 * 1024)
        manager = FakeManager()
        item = clip()

        async def scenario():
            paths = await asyncio.gather(*(cache.ensure_clip(manager, item) for _ in range(6)))
            again = await cache.ensure_clip(manager, item)
            return paths, again

        paths, again = asyncio.run(scenario())
        check(manager.fetches == 1, "six concurrent requests for one clip fetch it once")
        check(len({str(p) for p in paths}) == 1 and paths[0] == again, "they all get the same file")
        check(paths[0].read_bytes() == manager.payload, "the file holds what Blink sent")
        check(not list(pathlib.Path(tmp).glob("*.part")), "no partial file is left behind")
        check(cache.metadata(clip_id(item))["camera_name"] == "Front Door",
              "what the listing said is kept beside the clip, for filenames after a restart")


def test_downloads_are_serialised() -> None:
    print("\ndownloads never overlap")
    with tempfile.TemporaryDirectory() as tmp:
        cache = ClipCache(pathlib.Path(tmp), 100 * 1024 * 1024)
        manager = FakeManager()
        items = [clip("Cam", offset) for offset in range(8)]

        async def scenario():
            await asyncio.gather(*(cache.ensure_clip(manager, item) for item in items))

        asyncio.run(scenario())
        check(manager.fetches == 8, "eight distinct clips are each fetched")
        check(manager.max_in_flight == 1, "but never more than one at a time")


def test_empty_download_is_not_cached() -> None:
    print("\nan empty answer is not a clip")
    with tempfile.TemporaryDirectory() as tmp:
        cache = ClipCache(pathlib.Path(tmp), 100 * 1024 * 1024)
        manager = FakeManager(payload=b"")
        try:
            asyncio.run(cache.ensure_clip(manager, clip()))
            raised = False
        except RuntimeError:
            raised = True
        check(raised, "an empty body raises rather than caching nothing")
        check(not list(pathlib.Path(tmp).glob("*.mp4")), "and leaves no empty file to serve later")


def test_prune() -> None:
    print("\nthe cache is bounded, oldest first")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        now = time.time()
        for index in range(5):
            path = root / f"clip{index}.mp4"
            path.write_bytes(b"x" * 1000)
            os.utime(path, (now - 100 + index * 10, now - 100 + index * 10))
        keep = root / "clip0.mp4"

        removed = prune_directory(root, 2500, keep={keep})
        names = sorted(p.name for p in removed)
        # 5000 bytes over a 2500 cap: clip0 is kept, so clip1-3 go to get under it.
        check(names == ["clip1.mp4", "clip2.mp4", "clip3.mp4"], f"the oldest go first, skipping what is kept ({names})")
        check(keep.exists(), "a file being handed out is never evicted")
        check(prune_directory(root, 100 * 1024 * 1024) == [], "a cache under its cap loses nothing")
        check(prune_directory(root / "missing", 1) == [], "a missing directory is not an error")

        cache = ClipCache(root, 10 * 1024 * 1024)
        clip_path = root / "clip4.mp4"
        old = clip_path.stat().st_mtime
        os.utime(clip_path, (old - 3600, old - 3600))
        cache.cached_clip("clip4")
        check(clip_path.stat().st_mtime > old - 3600, "serving a cached clip marks it recently used")


def test_default_location() -> None:
    print("\nwhere the cache lands")
    base = pathlib.Path("/etc/blink-liveview-proxy")
    check(
        clip_cache_dir({"liveview_cache_dir": "/var/lib/blink-liveview-proxy/liveviews"}, base)
        == pathlib.Path("/var/lib/blink-liveview-proxy/clips"),
        "an older config.json gets a clips/ directory beside its live-view cache, not beside /etc",
    )
    check(
        clip_cache_dir({"liveview_cache_dir": "x", "clip_cache_dir": "/data/clips"}, base)
        == pathlib.Path("/data/clips"),
        "a configured directory wins",
    )
    check(
        clip_cache_dir({"liveview_cache_dir": ".runtime/p/liveviews", "clip_cache_dir": None}, base)
        == base / ".runtime/p/clips",
        "a relative live-view cache resolves against the config directory first",
    )


def test_thumbnail_arguments() -> None:
    print("\nthe thumbnail is one frame, scaled, as JPEG")
    args = thumbnail_args("ffmpeg", "warning", pathlib.Path("in.mp4"), pathlib.Path("out.jpg.part"))
    check(args[0] == "ffmpeg" and args[-1] == "out.jpg.part", "binary first, target last")
    check("-frames:v" in args and args[args.index("-frames:v") + 1] == "1", "exactly one frame")
    check(any(value.startswith("scale=") for value in args), "scaled down for a list row")
    check("mjpeg" in args, "the codec is named, so the .part target cannot confuse ffmpeg")
    check("-y" in args, "a stale partial is overwritten, not refused")


def test_thumbnail_with_ffmpeg() -> None:
    print("\ncutting a real frame (needs ffmpeg)")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        check(True, "skipped: ffmpeg is not installed here")
        return

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        source = root / "source.mp4"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=duration=1:size=640x360:rate=24",
             "-pix_fmt", "yuv420p", str(source)],
            check=True,
        )
        manager = FakeManager(payload=source.read_bytes())
        cache = ClipCache(root / "cache", 100 * 1024 * 1024)
        item = clip()

        path = asyncio.run(cache.ensure_thumbnail(manager, item, ffmpeg=ffmpeg, loglevel="error"))
        check(path.exists() and path.stat().st_size > 0, "a thumbnail file is produced")
        check(path.read_bytes()[:3] == b"\xff\xd8\xff", "and it is a JPEG")
        check(manager.fetches == 1, "cut from the cached clip, not a second download")

        again = asyncio.run(cache.ensure_thumbnail(manager, item, ffmpeg=ffmpeg, loglevel="error"))
        check(again == path and manager.fetches == 1, "asking again costs nothing")

        broken = root / "cache2"
        bad = FakeManager(payload=b"not a video at all")
        cache2 = ClipCache(broken, 100 * 1024 * 1024)
        try:
            asyncio.run(cache2.ensure_thumbnail(bad, item, ffmpeg=ffmpeg, loglevel="error"))
            raised = False
        except RuntimeError:
            raised = True
        check(raised, "a clip ffmpeg cannot read raises rather than caching a blank")
        check(not list(broken.glob("*.jpg")) and not list(broken.glob("*.part")),
              "and leaves no thumbnail or partial behind")


def main() -> int:
    for test in (
        test_fetch_once,
        test_downloads_are_serialised,
        test_empty_download_is_not_cached,
        test_prune,
        test_default_location,
        test_thumbnail_arguments,
        test_thumbnail_with_ffmpeg,
    ):
        test()
    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\nfailed:")
        for name in FAILURES:
            print(f"  {name}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
