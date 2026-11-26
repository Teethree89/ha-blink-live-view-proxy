"""When the first HLS playlist is served: not until it lists enough segments.

Issue #91: iPhones on iOS 18 read a one-segment live playlist, reloaded it
once, and then never asked for a segment. A shell script stands in for ffmpeg
and writes the playlist the way ffmpeg does, one segment a second.

Run from the repo root: python tests/test_hls_start.py
"""

import asyncio
import logging
import sys
import tempfile
import time
import types
from pathlib import Path

_PROXY = Path(__file__).resolve().parent.parent / "addon" / "proxy"

_package = types.ModuleType("blink_proxy")
_package.__path__ = [str(_PROXY / "blink_proxy")]
sys.modules["blink_proxy"] = _package

_blink = types.ModuleType("blink_proxy.blink")


class BlinkStreamBroker:  # only referenced in a type annotation
    pass


class LiveViewHandle:  # same
    pass


_blink.BlinkStreamBroker = BlinkStreamBroker
_blink.LiveViewHandle = LiveViewHandle
sys.modules["blink_proxy.blink"] = _blink

_config = types.ModuleType("blink_proxy.config")


def _resolve_path(value, base):
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


_config.resolve_path = _resolve_path
sys.modules["blink_proxy.config"] = _config

from blink_proxy.constants import DEFAULT_CONFIG, LOGGER_NAME  # noqa: E402
from blink_proxy.hls import HlsManager, HlsSession  # noqa: E402

# Stand-in ffmpeg: a playlist that gains a segment every half second, written
# through a temporary file and renamed, as ffmpeg's HLS muxer does. It stops
# growing at $STOP_AT segments.
GROWER = """#!/bin/sh
playlist=""
for arg in "$@"; do
  case "$arg" in *.m3u8) playlist="$arg" ;; esac
done
n=0
while [ "$n" -lt {stop_at} ]; do
  sleep 0.5
  n=$((n + 1))
  {{
    echo '#EXTM3U'
    echo '#EXT-X-VERSION:3'
    echo '#EXT-X-TARGETDURATION:1'
    echo '#EXT-X-MEDIA-SEQUENCE:0'
    i=0
    while [ "$i" -lt "$n" ]; do
      echo '#EXTINF:1.000456,'
      printf 'segment_%05d.ts\\n' "$i"
      i=$((i + 1))
    done
  }} > "$playlist.tmp"
  mv "$playlist.tmp" "$playlist"
done
sleep 30
"""

FAILURES: list[str] = []


def check(condition: bool, label: str, detail: object = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILURES.append(label)


class FakeLiveView:
    tcp_url = "tcp://127.0.0.1:1"

    async def close(self):
        pass


class FakeBroker:
    async def start_liveview(self, slug):
        return FakeLiveView()


class Captured(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


async def serve_first_playlist(tmp: Path, stop_at: int, extra: dict) -> tuple[int, float, list[str]]:
    """Start a session and time wait_ready(). Returns (segments listed, seconds, log lines)."""
    ffmpeg = tmp / "fake-ffmpeg"
    ffmpeg.write_text(GROWER.format(stop_at=stop_at))
    ffmpeg.chmod(0o755)
    config = {"ffmpeg": str(ffmpeg), "hls_dir": str(tmp / "hls"), "hls_start_timeout": 4, **extra}
    session = HlsSession("cam", HlsManager(FakeBroker(), config, tmp))
    captured = Captured()
    logging.getLogger(LOGGER_NAME).addHandler(captured)
    try:
        await session.start()
        started = time.monotonic()
        await session.wait_ready()
        elapsed = time.monotonic() - started
        listed = session._segment_count()
    finally:
        logging.getLogger(LOGGER_NAME).removeHandler(captured)
        await session.stop()
    return listed, elapsed, captured.lines


async def main() -> int:
    print("first HLS playlist")
    check(DEFAULT_CONFIG.get("hls_min_segments") == 3,
          "by default the first playlist waits for three segments")

    with tempfile.TemporaryDirectory() as raw:
        listed, elapsed, _lines = await serve_first_playlist(Path(raw), 8, {})
    check(listed >= 3, "it is not served with fewer", f"{listed} listed")
    check(1.3 < elapsed < 3.0, "and is served as soon as the third arrives, not later",
          f"{elapsed:.1f}s")

    with tempfile.TemporaryDirectory() as raw:
        listed, elapsed, _lines = await serve_first_playlist(Path(raw), 8, {"hls_min_segments": 0})
    check(listed >= 1 and elapsed < 1.2, "0 restores serving it the moment it exists",
          f"{listed} listed after {elapsed:.1f}s")

    with tempfile.TemporaryDirectory() as raw:
        listed, elapsed, lines = await serve_first_playlist(Path(raw), 2, {})
    check(listed == 2 and 3.5 < elapsed < 5.0,
          "a stream that never reaches three is still served at the start timeout, not failed",
          f"{listed} listed after {elapsed:.1f}s")
    check(any("listed 2 of 3 segments" in line for line in lines),
          "and the log says so", lines)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failed: {', '.join(FAILURES)}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
