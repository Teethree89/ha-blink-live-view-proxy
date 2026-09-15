"""The lamp rides the live-view session, and the camera answers on it.

These pin the bytes. An inline command is a nine-byte IMMI header with the
command in the sequence field and no payload; an accessory message from the
camera with 0 or 1 in that field is the lamp's state. Nothing here opens a
socket. Run from the repo root:

    python tests/test_liveview_commands.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "proxy"))

from blink_proxy import blink as blink_mod  # noqa: E402
from blink_proxy.constants import (  # noqa: E402
    IMMI_DATA_FLAG_ACCESSORY_MESSAGE,
    IMMI_DATA_FLAG_INLINE_LV_CMD,
    LIVEVIEW_INLINE_COMMAND_LIGHTS_OFF,
    LIVEVIEW_INLINE_COMMAND_LIGHTS_ON,
)
from blink_proxy.rtsp import BlinkRtspLiveStream  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


class FakeWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


class FakeCamera:
    serial = "SERIAL0001"


RESPONSE = {
    "command_id": 1,
    "polling_interval": 15,
    "server": "immis://198.51.100.9:443/abc__IMDS_x?client_id=9",
    "liveview_token": "token",
}


def frame(msgtype: int, sequence: int, payload: bytes = b"") -> bytes:
    return bytes([msgtype]) + sequence.to_bytes(4, "big") + len(payload).to_bytes(4, "big") + payload


def new_stream() -> blink_mod.TokenAwareBlinkLiveStream:
    return blink_mod.TokenAwareBlinkLiveStream(FakeCamera(), RESPONSE, send_token=True)


def test_inline_command_bytes() -> None:
    print("inline command framing")
    check(IMMI_DATA_FLAG_INLINE_LV_CMD == 0x14, "inline commands are msgtype 0x14")
    check(LIVEVIEW_INLINE_COMMAND_LIGHTS_ON == 1 and LIVEVIEW_INLINE_COMMAND_LIGHTS_OFF == 2,
          "lights on is command 1, lights off is command 2")
    stream = new_stream()
    writer = FakeWriter()
    stream.target_writer = writer
    asyncio.run(stream.send_inline_command(LIVEVIEW_INLINE_COMMAND_LIGHTS_ON))
    check(bytes(writer.buffer) == b"\x14\x00\x00\x00\x01\x00\x00\x00\x00",
          "lights on is a nine-byte header: 0x14, sequence 1, length 0")

    async def through_handle() -> bytes:
        handle_stream = new_stream()
        handle_writer = FakeWriter()
        handle_stream.target_writer = handle_writer
        handle = blink_mod.LiveViewHandle(
            stream=handle_stream, feed_task=asyncio.create_task(asyncio.sleep(0)), config={}
        )
        await handle.set_flood_light(True)
        await handle.set_flood_light(False)
        return bytes(handle_writer.buffer)

    sent = asyncio.run(through_handle())
    check(sent == frame(0x14, 1) + frame(0x14, 2), "the handle sends on as 1 and off as 2, nothing else")


def test_accessory_report() -> None:
    print("accessory messages")
    check(IMMI_DATA_FLAG_ACCESSORY_MESSAGE == 0x15, "the camera reports on msgtype 0x15")

    async def run(frames: list[bytes]) -> tuple[blink_mod.TokenAwareBlinkLiveStream, FakeWriter]:
        stream = new_stream()
        reader = asyncio.StreamReader()
        for item in frames:
            reader.feed_data(item)
        reader.feed_eof()
        stream.target_reader = reader
        stream.target_writer = FakeWriter()
        client = FakeWriter()
        stream.clients = [client]
        await stream.recv()
        return stream, client

    ts_packet = b"\x47" + bytes(187)
    stream, client = asyncio.run(run([frame(0x15, 6), frame(0x15, 1), frame(0x00, 5, ts_packet)]))
    check(stream.flood_light is True, "an accessory message with sequence 1 means the lamp is on")
    check(bytes(client.buffer) == ts_packet, "and the media frames around it still reach the client")
    stream, _ = asyncio.run(run([frame(0x15, 0)]))
    check(stream.flood_light is False, "sequence 0 means the lamp is off")
    stream, _ = asyncio.run(run([frame(0x15, 6), frame(0x18, 4)]))
    check(stream.flood_light is None, "a siren report or a session message says nothing about the lamp")
    check(new_stream().flood_light is None, "before the camera has spoken the lamp state is unknown")

    async def handle_state() -> tuple[bool | None, bool | None]:
        lit = new_stream()
        lit.flood_light = True
        immi = blink_mod.LiveViewHandle(
            stream=lit, feed_task=asyncio.create_task(asyncio.sleep(0)), config={}
        )
        rtsp = blink_mod.LiveViewHandle(
            stream=BlinkRtspLiveStream(FakeCamera(), {"server": "rtsps://198.51.100.9/x"}),
            feed_task=asyncio.create_task(asyncio.sleep(0)),
            config={},
        )
        return immi.flood_light, rtsp.flood_light

    immi_state, rtsp_state = asyncio.run(handle_state())
    check(immi_state is True, "the handle exposes what its stream heard")
    check(rtsp_state is None, "an RTSP session never knows the lamp")


def test_rtsp_refuses() -> None:
    print("RTSP")
    stream = BlinkRtspLiveStream(FakeCamera(), {"server": "rtsps://198.51.100.9/x"})
    try:
        asyncio.run(stream.send_inline_command(LIVEVIEW_INLINE_COMMAND_LIGHTS_ON))
        check(False, "an RTSP session refuses an inline command by name")
    except NotImplementedError as err:
        check("RTSP" in str(err), "an RTSP session refuses an inline command by name")


def main() -> int:
    test_inline_command_bytes()
    test_accessory_report()
    test_rtsp_refuses()
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
