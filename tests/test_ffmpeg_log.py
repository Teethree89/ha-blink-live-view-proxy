"""ffmpeg's stderr has to survive long enough to explain a failed HLS start.

No Blink account, no camera, no network, and nothing to install: a shell script
stands in for ffmpeg, and the two modules that would drag in blinkpy, aiohttp
and certifi are stubbed, the same way test_playlist.py avoids importing Home
Assistant. Run from the repo root:

    python tests/test_ffmpeg_log.py
"""

import asyncio
import logging
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

LOGGER_NAME = "blink_liveview_proxy"
FAILURE = "Invalid data found when processing input"


class FakeLiveView:
    def __init__(self, url):
        self.tcp_url = url
        self.closed = False

    async def close(self):
        self.closed = True


class FakeBroker:
    async def start_liveview(self, slug):
        return FakeLiveView("tcp://127.0.0.1:1")


class Captured(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def make_session(tmp, script):
    ffmpeg = tmp / "fake-ffmpeg"
    ffmpeg.write_text(script)
    ffmpeg.chmod(0o755)
    config = {
        "ffmpeg": str(ffmpeg),
        "hls_dir": str(tmp / "hls"),
        "hls_start_timeout": 3,
    }
    return HlsSession("testcam", HlsManager(FakeBroker(), config, tmp))


async def run_case(title, script, settle, expect, needle):
    print(f"== {title} ==")
    with tempfile.TemporaryDirectory() as raw:
        session = make_session(Path(raw), script)
        captured = Captured()
        logging.getLogger(LOGGER_NAME).addHandler(captured)
        await session.start()
        if settle:
            await asyncio.sleep(settle)
        try:
            await session.wait_ready()
        except expect as error:
            print(f"  raised: {error}")
        else:
            raise AssertionError(f"wait_ready should have raised {expect.__name__}")
        finally:
            logging.getLogger(LOGGER_NAME).removeHandler(captured)

        hits = [line for line in captured.lines if needle in line]
        assert hits, f"expected {needle!r} in a log line; saw {captured.lines}"
        print(f"  logged: {hits[0][:104]}")
        print("  PASS\n")
        await session.stop()


async def main():
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.ERROR)
    logger.propagate = False  # keep the captured records out of the console

    await run_case(
        "TEST 1: ffmpeg exits, its stderr should reach the log",
        f"#!/bin/sh\necho '{FAILURE}' >&2\nexit 1\n",
        0.5,
        RuntimeError,
        FAILURE,
    )
    await run_case(
        "TEST 2: no playlist while ffmpeg lives, the tail should still show",
        "#!/bin/sh\necho 'could not find codec parameters' >&2\nsleep 30\n",
        0,
        TimeoutError,
        "could not find codec parameters",
    )
    await run_case(
        "TEST 3: ffmpeg dies saying nothing, the message must not be blank",
        "#!/bin/sh\nexit 1\n",
        0.5,
        RuntimeError,
        "stderr was empty",
    )
    print("all ffmpeg log tests passed")


asyncio.run(main())
