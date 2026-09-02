"""Pin the ffmpeg command line. A shell script stands in for ffmpeg.

Run from the repo root: python tests/test_ffmpeg_args.py
"""

import asyncio
import sys
import tempfile
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

from blink_proxy.hls import HlsManager, HlsSession  # noqa: E402

# Stand-in ffmpeg: record argv next to the playlist, write it, stay alive.
RECORDER = """#!/bin/sh
eval playlist=\\${$#}
printf '%s\\n' "$@" > "$(dirname "$playlist")/args.txt"
echo '#EXTM3U' > "$playlist"
sleep 30
"""


class FakeLiveView:
    def __init__(self, url):
        self.tcp_url = url

    async def close(self):
        pass


class FakeBroker:
    async def start_liveview(self, slug):
        return FakeLiveView("tcp://127.0.0.1:1")


async def record_args(extra_config):
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        ffmpeg = tmp / "fake-ffmpeg"
        ffmpeg.write_text(RECORDER)
        ffmpeg.chmod(0o755)
        config = {"ffmpeg": str(ffmpeg), "hls_dir": str(tmp / "hls"), "hls_start_timeout": 3}
        config.update(extra_config)
        session = HlsSession("testcam", HlsManager(FakeBroker(), config, tmp))
        await session.start()
        await session.wait_ready()
        args = (session.directory / "args.txt").read_text().splitlines()
        await session.stop()
        return args


def option(args, name):
    """The value following a flag, or None if the flag is absent."""
    return args[args.index(name) + 1] if name in args else None


def check(title, condition, detail):
    print(f"== {title} ==")
    assert condition, detail
    print(f"  {detail}\n  PASS\n")


async def main():
    args = await record_args({})
    inputs = args[: args.index("-i")]

    check(
        "the opening keyframe is not thrown away",
        "nobuffer" not in args,
        "-fflags nobuffer is absent (it discards the packets read during "
        "analysis, and Blink's first keyframe is among them)",
    )
    check(
        "stream analysis is bounded",
        option(inputs, "-probesize") == "1000000"
        and option(inputs, "-analyzeduration") == "500000",
        f"input options: -probesize {option(inputs, '-probesize')} "
        f"-analyzeduration {option(inputs, '-analyzeduration')}, both before -i",
    )
    check(
        "the analysis window can be widened per install",
        option(
            (await record_args({"ffmpeg_analyzeduration": 2_000_000}))[: args.index("-i") + 2],
            "-analyzeduration",
        )
        == "2000000",
        "ffmpeg_analyzeduration in the config reaches the command line",
    )
    check(
        "the stream is still copied, not re-encoded",
        option(args, "-c") == "copy",
        "-c copy is unchanged",
    )
    print("all ffmpeg argument checks passed")


if __name__ == "__main__":
    asyncio.run(main())
