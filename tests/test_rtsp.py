"""Stub-server tests for BlinkRtspLiveStream. No Blink account or camera.

    python tests/test_rtsp.py        (from the repo root)

The stub mimics Blink, including all three of the spec violations this module
exists to work around:

  * it answers **every** request with ``CSeq: 1``
  * it omits the ``Session`` header on SETUP
  * it omits the ``Transport`` header on SETUP

and - the point of test 1 - it starts sending as soon as SETUP is answered,
which is *during* the handshake.

Origin
------
The harness, the stub and the test ideas are bbolinger's, contributed in review
of PR #4:

    https://github.com/bbolinger/ha-blink-live-view-proxy/tree/rtsp-extras-reference/tests

That version runs against his own module (``BlinkRtspRelay``); this one is
rewritten for ``BlinkRtspLiveStream``. Two of his four cases are carried over -
the two that exercise this module. The other two (reconnect after an early
drop, and *no* reconnect once a session has settled) assume reconnect logic
that this module deliberately does not have; they remain on his branch.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import sys

# proxy/ and addon/proxy/ are byte-identical and CI enforces that, so testing
# one covers both.
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "proxy"))
logging.basicConfig(level=logging.INFO, format="  %(levelname)s %(message)s")

from blink_proxy import rtsp as R  # noqa: E402

URL = "rtsps://stub.invalid:443/session__IMDS_1?client_id=82&blinkRTSP=true"

SDP = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=name\r\nc=IN IP4 127.0.0.1\r\n"
       "t=0 0\r\na=tool:immedia_isi108 0.0.1\r\na=control:*\r\n"
       "m=video 5002 RTP/AVP 33\r\na=rtpmap:33 MP2T/90000\r\n"
       "a=control:trackID=1\r\na=range:npt=now-\r\n")

OK = b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n"      # Blink always says CSeq: 1


def frame(seq: int) -> bytes:
    """One interleaved RTP frame carrying a single 188-byte MPEG-TS packet."""
    ts = b"\x47" + bytes(187)
    rtp = bytes([0x80, 33]) + (seq % 65536).to_bytes(2, "big") + bytes(8) + ts
    return b"$" + bytes([0]) + len(rtp).to_bytes(2, "big") + rtp


def describe_reply() -> bytes:
    body = SDP.encode()
    return (b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Type: application/sdp\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)


class Stub:
    """A Blink-like RTSP server, as unhelpful as the original."""

    def __init__(self, frames_after_setup: int = 0, frames_after_play: int = 5,
                 stream_seconds: float = 0.0, send_session: bool = False) -> None:
        self.connections = 0
        self.frames_after_setup = frames_after_setup
        self.frames_after_play = frames_after_play
        self.stream_seconds = stream_seconds
        self.send_session = send_session
        self.server: asyncio.AbstractServer | None = None

    async def handle(self, reader, writer) -> None:
        self.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                method = head.split(b" ", 1)[0].decode()
                if method == "DESCRIBE":
                    writer.write(describe_reply())
                elif method == "SETUP":
                    if self.send_session:
                        writer.write(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n"
                                     b"Session: immedia0\r\n"
                                     b"Transport: RTP/AVP/TCP;interleaved=0-1\r\n\r\n")
                    else:
                        writer.write(OK)          # no Session, no Transport
                    await writer.drain()
                    for i in range(self.frames_after_setup):
                        writer.write(frame(i))    # video before PLAY
                elif method == "PLAY":
                    writer.write(OK)
                    await writer.drain()
                    for i in range(self.frames_after_play):
                        writer.write(frame(i))
                    await writer.drain()
                    if self.stream_seconds:
                        deadline = (asyncio.get_running_loop().time()
                                    + self.stream_seconds)
                        i = 0
                        while asyncio.get_running_loop().time() < deadline:
                            writer.write(frame(i))
                            i += 1
                            if i % 20 == 0:
                                await writer.drain()
                                await asyncio.sleep(0.02)
                    await asyncio.sleep(3)
                    return
                else:
                    writer.write(OK)
                await writer.drain()
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()


async def start_stub(stub: Stub) -> int:
    stub.server = await asyncio.start_server(stub.handle, "127.0.0.1", 0)
    return int(stub.server.sockets[0].getsockname()[1])


def patch_connect(port: int):
    """Force the module's outbound connection to the stub, in plain TCP."""
    real = asyncio.open_connection

    async def fake(host=None, p=None, ssl=None, **kw):
        return await real("127.0.0.1", port)

    asyncio.open_connection = fake
    return real


def make_stream() -> R.BlinkRtspLiveStream:
    return R.BlinkRtspLiveStream(camera=None, response={"server": URL})


async def collect(real, stream, seconds: float) -> bytearray:
    """Attach a consumer and return the bytes it receives."""
    got = bytearray()
    port = stream.socket.getsockname()[1]
    reader, _ = await real("127.0.0.1", port)

    async def drain():
        with contextlib.suppress(Exception):
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                got.extend(chunk)

    task = asyncio.create_task(drain())
    await asyncio.sleep(seconds)
    task.cancel()
    return got


def ts_clean(buf: bytes) -> bool:
    """True if every 188-byte packet starts with the MPEG-TS sync byte."""
    return (len(buf) > 0 and len(buf) % 188 == 0
            and all(buf[i] == 0x47 for i in range(0, len(buf), 188)))


# --------------------------------------------------------------------------
async def test_frames_during_handshake() -> bool:
    """The failure mode this module was written for.

    Blink starts sending as soon as SETUP is answered. A reader that blindly
    scans for the next CRLFCRLF eats those frames and loses framing. This stub
    also omits Session and Transport, as Blink does on the #4 account.
    """
    print("\n== TEST 1: data frames arrive mid-handshake ==")
    stub = Stub(frames_after_setup=8, frames_after_play=0, stream_seconds=2.5)
    port = await start_stub(stub)
    real = patch_connect(port)
    try:
        stream = make_stream()
        await stream.start()
        await stream.connect()
        handshake_ok = stream._running
        task = asyncio.create_task(stream.feed())
        got = await collect(real, stream, 2.5)
        stream.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        print(f"  handshake survived:         {handshake_ok}")
        print(f"  relayed:                    {len(got)} bytes "
              f"({len(got) // 188} TS packets)")
        print(f"  every packet starts 0x47:   {ts_clean(got)}")
        ok = handshake_ok and ts_clean(got) and len(got) > 20 * 188
        print(f"  {'PASS' if ok else 'FAIL'}  no frames lost or corrupted")
        return ok
    finally:
        asyncio.open_connection = real
        stub.server.close()


async def test_backpressure() -> bool:
    """A consumer that stops reading must not grow the heap without limit."""
    print("\n== TEST 2: consumer never reads, should be dropped ==")
    R.MAX_CONSUMER_BACKLOG_BYTES = 50_000
    # A burst, not a trickle. A few hundred KB/s spread over time disappears
    # into the socket's kernel buffer, no backlog ever reaches the transport,
    # and the test measures nothing. 40k frames is about 8 MB at once.
    stub = Stub(frames_after_play=40_000)
    port = await start_stub(stub)
    real = patch_connect(port)
    try:
        stream = make_stream()
        await stream.start()
        await stream.connect()
        task = asyncio.create_task(stream.feed())
        _r, _w = await real("127.0.0.1", stream.socket.getsockname()[1])  # never reads
        await asyncio.sleep(4)
        remaining = len(stream._consumers)
        stream.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        print(f"  consumers still attached:   {remaining}")
        ok = remaining == 0
        print(f"  {'PASS' if ok else 'FAIL'}  slow consumer dropped")
        return ok
    finally:
        asyncio.open_connection = real
        R.MAX_CONSUMER_BACKLOG_BYTES = 4 * 1024 * 1024
        stub.server.close()


async def main() -> int:
    results = [
        await test_frames_during_handshake(),
        await test_backpressure(),
    ]
    print("\n%d/%d passed" % (sum(1 for r in results if r), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
