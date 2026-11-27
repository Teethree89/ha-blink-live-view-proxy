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
playlist=""
for arg in "$@"; do
  case "$arg" in *.m3u8) playlist="$arg" ;; esac
done
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
        config = {"ffmpeg": str(ffmpeg), "hls_dir": str(tmp / "hls"), "hls_start_timeout": 3, "hls_min_segments": 0}
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


def split_outputs(args):
    """The two outputs: the HLS playlist, then the cached copy.

    Split at the playlist rather than at each -f, because an output's codec
    options come before its -f and would otherwise land in the wrong half.
    """
    start = args.index("-i") + 2
    cut = next(i for i, a in enumerate(args) if a.endswith(".m3u8")) + 1
    return args[start:cut], args[cut:]


async def main():
    args = await record_args({})
    inputs = args[: args.index("-i")]

    # An HLS session has to leave the same cached copy the MPEG-TS path does,
    # or saving a live view has nothing to finalize on a client that plays HLS -
    # which is every iPhone, since they have no MSE and never take the other
    # route. It used to fail there, and "Save MP4" quietly handed back an
    # older session's recording instead.
    hls_out, cache_out = split_outputs(args)
    check(
        "the live view is cached alongside the playlist",
        option(hls_out, "-f") == "hls" and option(cache_out, "-f") == "mpegts",
        f"an hls playlist and an mpegts cache ({option(hls_out, '-f')}, {option(cache_out, '-f')})",
    )
    check(
        "the cached copy lands in the live-view cache",
        cache_out[-1].endswith(".ts.part") and "liveviews" in cache_out[-1],
        f"written to a .part in the cache directory ({Path(cache_out[-1]).name})",
    )

    check(
        "both outputs are straight copies of what Blink sent",
        option(hls_out, "-c") == "copy" and option(cache_out, "-c") == "copy",
        f"-c {option(hls_out, '-c')} for the playlist, "
        f"-c {option(cache_out, '-c')} for the cache",
    )

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
        "the video stream is mapped whether or not the analysis delimited it",
        args[args.index("-i") + 2 : args.index("-i") + 6]
        == ["-map", "0:v:0", "-map", "0:a:0?"],
        "-map 0:v:0 -map 0:a:0? right after the input (auto-mapping drops a "
        "video stream the analysis never delimited)",
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
        "the stream is copied, not re-encoded",
        option(args, "-c") == "copy" and option(args, "-hls_list_size") == "8",
        "-c copy with an eight segment playlist",
    )
    check(
        "copied segments are cut on the clock",
        "split_by_time" in option(args, "-hls_flags").split("+"),
        "split_by_time is in -hls_flags so segments are 1 s, not one GOP",
    )
    keyframed = await record_args({"hls_keyframe_segments": True})
    check(
        "hls_keyframe_segments cuts only at keyframes",
        "split_by_time" not in option(keyframed, "-hls_flags").split("+")
        and option(keyframed, "-c") == "copy",
        "split_by_time is dropped and the stream is still copied (Apple 541007, #91)",
    )
    check(
        "no encoder is configured at all",
        not any(a.startswith("-c:v") or a == "libx264" for a in args),
        "nothing on the command line asks ffmpeg to encode video",
    )

    print("all ffmpeg argument checks passed")


if __name__ == "__main__":
    asyncio.run(main())
