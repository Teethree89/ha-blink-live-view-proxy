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
        "by default the stream is copied, not re-encoded",
        option(args, "-c") == "copy" and option(args, "-hls_list_size") == "4",
        "-c copy with a four segment playlist, as before",
    )

    fast = await record_args({"hls_transcode": True})
    outputs = fast[fast.index("-i") + 2 :]
    check(
        "low latency re-encodes the video and copies the audio",
        option(outputs, "-c:v") == "libx264" and option(outputs, "-c:a") == "copy",
        f"-c:v {option(outputs, '-c:v')} -c:a {option(outputs, '-c:a')}",
    )
    check(
        "low latency forces a keyframe every second",
        option(outputs, "-force_key_frames") == "expr:gte(t,n_forced*1)"
        and option(outputs, "-hls_time") == "1",
        "one forced keyframe per second, one second segments",
    )
    check(
        "the re-encode has a bitrate ceiling",
        option(outputs, "-maxrate") == "2000k" and option(outputs, "-bufsize") == "2000k",
        "-maxrate 2000k -bufsize 2000k (an uncapped encode reached 8 Mbit/s "
        "on a busy scene and the phone could not keep up)",
    )
    check(
        "the output frame rate is pinned",
        option(outputs, "-r") == "24",
        "-r 24 (left to guess from a stream that opens with a gap, ffmpeg "
        "took 90000 fps and never finished a segment)",
    )
    check(
        "one second segments come with a longer playlist",
        option(outputs, "-hls_list_size") == "6",
        "six one second segments; four stalled iOS",
    )
    check(
        "the frame rate pin is configurable",
        option(await record_args({"hls_transcode": True, "hls_frame_rate": 30}), "-r") == "30",
        "hls_frame_rate in the config reaches the command line",
    )
    print("all ffmpeg argument checks passed")


if __name__ == "__main__":
    asyncio.run(main())
