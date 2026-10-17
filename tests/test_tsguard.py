"""A camera that announces audio and sends none must not stall every consumer.

Issue #80: one of two Outdoor 4 cameras never started in the dashboard dialog,
while Home Assistant's own more-info dialog played it. The camera's PMT
announced an audio stream and no audio packet ever came. mpegts.js, which
plays the dialog, waits for the first frame of every announced track and so
buffers the video for ever; ffmpeg cannot write an audio stream whose sample
rate it never learned, so the HLS an iPhone plays and Save MP4 fail outright;
Home Assistant's own stream shrugs it off, which is why it looked like a fault
in one dialog.

These build transport streams by hand, so nothing here needs a camera, a
browser or ffmpeg - the last section runs the real ffmpeg commands only if one
is installed. Run from the repo root:

    python tests/test_tsguard.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "proxy"))

from blink_proxy import blink as blink_mod  # noqa: E402
from blink_proxy import tsguard  # noqa: E402
from blink_proxy.constants import DEFAULT_CONFIG  # noqa: E402
from blink_proxy.hls import HlsManager, HlsSession  # noqa: E402
from blink_proxy.rtsp import BlinkRtspLiveStream  # noqa: E402
from blink_proxy.tsguard import AudioGuard  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0

PMT_PID, VIDEO_PID, AUDIO_PID = 0x1000, 0x0100, 0x0101
H264, AAC_ADTS = 0x1B, 0x0F
PACKET = 188


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


# ---------------------------------------------------------------- stream kit
def reference_crc(data: bytes) -> int:
    """CRC-32/MPEG-2, written out bit by bit and independently of tsguard."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def ts(pid: int, payload: bytes, *, start: bool = False, counter: int = 0) -> bytes:
    assert len(payload) <= PACKET - 4
    header = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF, 0x10 | (counter & 0xF)])
    return header + payload.ljust(PACKET - 4, b"\xff")


def section(table_id: int, body: bytes) -> bytes:
    length = len(body) + 4
    head = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF])
    return head + body + reference_crc(head + body).to_bytes(4, "big")


def pat(counter: int = 0) -> bytes:
    body = bytes([0x00, 0x01, 0xC1, 0x00, 0x00, 0x00, 0x01, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF])
    return ts(0, b"\x00" + section(0x00, body), start=True, counter=counter)


def pmt(
    streams: tuple[tuple[int, int], ...] = ((H264, VIDEO_PID), (AAC_ADTS, AUDIO_PID)),
    pcr_pid: int = VIDEO_PID,
    padding: int = 0,
    counter: int = 0,
) -> bytes:
    body = bytes([0x00, 0x01, 0xC1, 0x00, 0x00, 0xE0 | (pcr_pid >> 8), pcr_pid & 0xFF, 0xF0, 0x00])
    for stream_type, pid in streams:
        body += bytes([stream_type, 0xE0 | (pid >> 8), pid & 0xFF, 0xF0, 0x00])
    body += bytes(padding)
    return ts(PMT_PID, b"\x00" + section(0x02, body), start=True, counter=counter)


def video(index: int) -> bytes:
    body = b"\x00\x00\x01\xe0\x00\x00\x80\x00\x00" + index.to_bytes(4, "big") + b"\xab" * 40
    return ts(VIDEO_PID, body, start=True, counter=index)


def audio(index: int) -> bytes:
    body = b"\x00\x00\x01\xc0\x00\x00\x80\x00\x00" + b"\xff\xf1" + index.to_bytes(4, "big") + b"\xcd" * 20
    return ts(AUDIO_PID, body, start=True, counter=index)


def stream(*, audio_sent: bool, audio_declared: bool = True, frames: int = 120, audio_from: int = 0) -> bytes:
    declared = ((H264, VIDEO_PID), (AAC_ADTS, AUDIO_PID)) if audio_declared else ((H264, VIDEO_PID),)
    out = [pat(), pmt(declared)]
    for index in range(frames):
        if index and index % 15 == 0:
            out += [pat(index), pmt(declared, counter=index)]
        out.append(video(index))
        if audio_sent and index >= audio_from and index % 2 == 0:
            out.append(audio(index))
    return b"".join(out)


def packets(data: bytes) -> list[bytes]:
    return [data[i : i + PACKET] for i in range(0, len(data), PACKET)]


def pid_of(packet: bytes) -> int:
    return ((packet[1] & 0x1F) << 8) | packet[2]


class Clock:
    """A clock that moves only when told to."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# What a camera sends, roughly: 100 kB a second is about 0.8 Mbit/s.
BYTES_PER_SECOND = 100_000


def run_guard(
    data: bytes, *, chunk: int = PACKET * 7, rate: int = BYTES_PER_SECOND, wait: float = 2.0
) -> tuple[list[bytes], AudioGuard, Clock]:
    """Push ``data`` in chunks as a camera would send it; collect each output.

    The clock moves with the bytes, so how long the guard has waited does not
    depend on how the stream happens to be cut into chunks.
    """
    clock = Clock()
    guard = AudioGuard("Driveway", wait, clock)
    outputs = []
    for start in range(0, len(data), chunk):
        piece = data[start : start + chunk]
        clock.now += len(piece) / rate
        outputs.append(guard.push(piece))
    outputs.append(guard.flush())
    return outputs, guard, clock


def pmt_streams(packet: bytes) -> tuple[int, list[tuple[int, int]]]:
    """(PCR pid, [(stream_type, pid)]) from a PMT packet, checking its CRC."""
    payload = packet[4:]
    sect = payload[1 : 1 + 3 + (((payload[2] & 0x0F) << 8) | payload[3])]
    assert reference_crc(sect[:-4]) == int.from_bytes(sect[-4:], "big"), "PMT CRC is wrong"
    pcr = ((sect[8] & 0x1F) << 8) | sect[9]
    position, end = 12 + (((sect[10] & 0x0F) << 8) | sect[11]), len(sect) - 4
    found = []
    while position + 5 <= end:
        found.append((sect[position], ((sect[position + 1] & 0x1F) << 8) | sect[position + 2]))
        position += 5 + (((sect[position + 3] & 0x0F) << 8) | sect[position + 4])
    return pcr, found


# ----------------------------------------------------------------- the tests
def test_crc() -> None:
    print("PSI checksum")
    check(tsguard.mpeg_crc32(b"123456789") == 0x0376E6E7, "CRC-32/MPEG-2 matches its published check value")
    sample = section(0x02, bytes(range(40)))
    check(tsguard.mpeg_crc32(sample) == 0, "a section that carries its own CRC checks to zero")
    check(tsguard.mpeg_crc32(sample[:-4]) == reference_crc(sample[:-4]), "and agrees with an independent implementation")


def test_healthy_passes_untouched() -> None:
    print("audio that arrives")
    data = stream(audio_sent=True)
    for chunk in (1, 7, 100, PACKET, PACKET * 7, 4096, len(data)):
        outputs, guard, _clock = run_guard(data, chunk=chunk)
        check(b"".join(outputs) == data and guard.mode == "pass", f"identical byte for byte in {chunk}-byte chunks")

    outputs, guard, _clock = run_guard(data)
    first = next(i for i, out in enumerate(outputs) if out)
    check(first <= 1, "released at the first chunk that carries audio, not after the wait")

    # Roughly 1.3 s of picture before the first audio packet: inside the wait.
    late = stream(audio_sent=True, audio_from=500, frames=1500)
    outputs, guard, _clock = run_guard(late)
    check(b"".join(outputs) == late and guard.mode == "pass", "audio that turns up inside the wait is left alone")


def test_no_announcement_passes() -> None:
    print("a stream that announces no audio")
    data = stream(audio_sent=False, audio_declared=False)
    outputs, guard, _clock = run_guard(data)
    check(b"".join(outputs) == data and guard.mode == "pass", "is forwarded untouched")
    check(bool(outputs[0]), "and at once, with nothing held back")


def test_announced_but_never_sent() -> None:
    print("audio announced and never sent (#80)")
    data = stream(audio_sent=False, frames=1500)
    outputs, guard, clock = run_guard(data)
    released = next(i for i, out in enumerate(outputs) if out)
    check(released > 10 and guard.mode == "strip", "the start of the stream is held, then the audio is declared absent")
    check(PACKET * 7 * (released + 1) / BYTES_PER_SECOND >= 2.0, "and not before the wait is up")

    out = b"".join(outputs)
    sent, received = packets(data), packets(out)
    check(len(sent) == len(received), "no packet is lost or added (there was no audio to drop)")
    changed = [i for i, (a, b) in enumerate(zip(sent, received)) if a != b]
    check(bool(changed) and all(pid_of(sent[i]) == PMT_PID for i in changed), "only PMT packets differ")
    shows = [pmt_streams(received[i]) for i in changed]
    check(all(s == (VIDEO_PID, [(H264, VIDEO_PID)]) for s in shows), "every PMT now lists the video alone, with a valid CRC")
    check(
        [r for r in received if pid_of(r) != PMT_PID] == [s for s in sent if pid_of(s) != PMT_PID],
        "every other packet, video and PAT, is byte for byte what the camera sent",
    )
    check(all(r[3] & 0x0F == s[3] & 0x0F for r, s in zip(received, sent)), "continuity counters are untouched")

    # The decision is final: audio that turns up afterwards is dropped, and the
    # PMT keeps saying video only, so the picture is never promised a track.
    more = stream(audio_sent=True, frames=60)
    tail = guard.push(more)
    pids = {pid_of(p) for p in packets(tail)}
    check(AUDIO_PID not in pids, "audio that arrives after the decision is dropped")
    check(
        all(pmt_streams(p)[1] == [(H264, VIDEO_PID)] for p in packets(tail) if pid_of(p) == PMT_PID),
        "and later PMTs are rewritten too",
    )


def test_audio_on_the_pcr_pid() -> None:
    print("a PCR carried on the audio stream")
    declared = ((H264, VIDEO_PID), (AAC_ADTS, AUDIO_PID))
    data = b"".join([pat(), pmt(declared, pcr_pid=AUDIO_PID)] + [video(i) for i in range(1500)])
    outputs, guard, _clock = run_guard(data)
    rewritten = next(p for p in packets(b"".join(outputs)) if pid_of(p) == PMT_PID)
    check(pmt_streams(rewritten) == (VIDEO_PID, [(H264, VIDEO_PID)]), "the PCR moves to a stream that is still there")


def test_chunking_does_not_matter() -> None:
    print("arbitrary chunk boundaries")
    data = stream(audio_sent=False, frames=1500)
    reference = b"".join(run_guard(data, chunk=PACKET * 7)[0])
    check(reference != data, "the reference run did rewrite the stream")
    for chunk in (1, 5, 187, 189, 1000, 3001):
        check(b"".join(run_guard(data, chunk=chunk)[0]) == reference, f"{chunk}-byte chunks give the same stream")


def test_leaves_alone_what_it_cannot_read() -> None:
    print("what it cannot read")
    # A PMT split over two packets cannot be rewritten. Fake one by making the
    # section claim more bytes than its packet holds.
    broken = bytearray(pmt())
    broken[4 + 1 + 2] = 0xFF  # section_length low byte: far past the payload
    data = b"".join([pat(), bytes(broken)] + [video(i) for i in range(1500)])
    outputs, guard, _clock = run_guard(data)
    check(b"".join(outputs) == data and guard.mode == "pass", "a PMT that spans packets is forwarded untouched")

    bare = b"".join(video(i) for i in range(1500))
    outputs, guard, _clock = run_guard(bare)
    check(b"".join(outputs) == bare, "a stream with no PMT at all is forwarded untouched once the wait is up")

    junk = b"\x00\x01\x02" * 10
    data = junk + stream(audio_sent=True)
    outputs, guard, _clock = run_guard(data)
    check(b"".join(outputs) == data, "leading junk before the first sync byte does not hide the audio")

    adaptive = bytearray(pmt())
    adaptive[3] = 0x30 | (adaptive[3] & 0x0F)  # adaptation field present
    check(tsguard.rewrite_pmt_packet(bytes(adaptive), {AUDIO_PID}) is None, "a PMT packet with an adaptation field is not rebuilt")


def test_switch_and_bounds() -> None:
    print("switch and bounds")
    data = stream(audio_sent=False, frames=1500)
    outputs, guard, _clock = run_guard(data, wait=0)
    check(b"".join(outputs) == data and guard.mode == "pass" and bool(outputs[0]), "a wait of zero turns the guard off")

    clock = Clock()
    guard = AudioGuard("x", 2.0, clock)
    half = data[: PACKET * 40]
    held = guard.push(half)
    check(held == b"" and guard.flush() == half, "a stream that ends inside the wait gives back everything it held")

    tsguard.MAX_HELD_BYTES, saved = PACKET * 10, tsguard.MAX_HELD_BYTES
    try:
        outputs, guard, _clock = run_guard(data, rate=10_000_000)
        check(b"".join(outputs) == data and guard.mode == "pass", "the hold is bounded, and gives up safely when it is full")
    finally:
        tsguard.MAX_HELD_BYTES = saved

    check(tsguard.audio_wait_from_config({}) == 2.0, "the wait defaults to two seconds")
    check(tsguard.audio_wait_from_config({"liveview_audio_wait_seconds": "0"}) == 0.0, "a numeric string is accepted")
    check(tsguard.audio_wait_from_config({"liveview_audio_wait_seconds": "soon"}) == 2.0, "garbage falls back to the default")
    check(DEFAULT_CONFIG["liveview_audio_wait_seconds"] == 2.0, "the key ships in the defaults")


# ------------------------------------------------------------------- wiring
class FakeWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False
        self.transport = None

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
    name = "Driveway"


RESPONSE = {
    "command_id": 1,
    "polling_interval": 15,
    "server": "immis://198.51.100.9:443/abc__IMDS_x?client_id=9",
    "liveview_token": "token",
}


def immi(payload: bytes) -> bytes:
    return b"\x00" + (1).to_bytes(4, "big") + len(payload).to_bytes(4, "big") + payload


def through_immi(data: bytes, *, wait: float) -> bytes:
    """What a client of the IMMI stream receives, with a clock that ticks per call."""

    async def go() -> bytes:
        stream_ = blink_mod.TokenAwareBlinkLiveStream(FakeCamera(), RESPONSE, send_token=True, audio_wait_seconds=wait)
        ticking = Clock()
        original = ticking.__call__

        def advance() -> float:
            ticking.now += 0.05
            return original()

        stream_._audio_guard = AudioGuard("Driveway", wait, advance)
        reader = asyncio.StreamReader()
        for start in range(0, len(data), PACKET * 7):
            reader.feed_data(immi(data[start : start + PACKET * 7]))
        reader.feed_eof()
        stream_.target_reader = reader
        stream_.target_writer = FakeWriter()
        client = FakeWriter()
        stream_.clients = [client]
        await stream_.recv()
        return bytes(client.buffer)

    return asyncio.run(go())


def through_rtsp(data: bytes, *, wait: float) -> bytes:
    stream_ = BlinkRtspLiveStream(FakeCamera(), {"server": "rtsps://198.51.100.9/x"}, audio_wait_seconds=wait)
    ticking = Clock()
    original = ticking.__call__

    def advance() -> float:
        ticking.now += 0.05
        return original()

    stream_._audio_guard = AudioGuard("Driveway", wait, advance)
    consumer = FakeWriter()
    stream_._consumers = [consumer]
    rtp_header = bytes([0x80, 33]) + bytes(10)
    for start in range(0, len(data), PACKET * 7):
        stream_._dispatch_frame(0, rtp_header + data[start : start + PACKET * 7])
    tail = stream_._audio_guard.flush()
    if tail:
        stream_._send_to_consumers(tail)
    return bytes(consumer.buffer)


def test_wiring() -> None:
    print("both stream types")
    healthy, absent = stream(audio_sent=True, frames=600), stream(audio_sent=False, frames=600)
    for name, run in (("IMMI", through_immi), ("RTSP", through_rtsp)):
        check(run(healthy, wait=2.0) == healthy, f"{name}: a stream with audio reaches its client untouched")
        out = run(absent, wait=2.0)
        shows = {pmt_streams(p)[1][-1] for p in packets(out) if pid_of(p) == PMT_PID}
        check(shows == {(H264, VIDEO_PID)}, f"{name}: a stream with no audio reaches its client announcing none")
        check(len(out) == len(absent), f"{name}: and loses none of its picture")
        check(run(absent, wait=0) == absent, f"{name}: with the guard off the camera's stream is passed as it came")

    stream_ = blink_mod.TokenAwareBlinkLiveStream(FakeCamera(), RESPONSE, send_token=True)
    check(stream_._audio_guard.wait_seconds == 2.0 and stream_._audio_guard.label == "Driveway", "an IMMI stream is guarded by default, under the camera's name")
    check(BlinkRtspLiveStream(FakeCamera(), {"server": "rtsps://x"})._audio_guard.wait_seconds == 2.0, "so is an RTSP one")


# --------------------------------------------------------- real ffmpeg, if any
def ffmpeg_sample() -> bytes | None:
    """A few seconds of real H.264 + AAC in MPEG-TS, or None without a capable ffmpeg."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return None
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
        "-t", "5", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-bf", "0",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "16000", "-ac", "1", "-f", "mpegts", "pipe:1",
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    return result.stdout if result.returncode == 0 and result.stdout else None


class _View:
    def __init__(self, url: str) -> None:
        self.tcp_url = url

    async def close(self) -> None:
        pass


class _Broker:
    async def start_liveview(self, slug: str) -> _View:
        return _View("tcp://127.0.0.1:1")


RECORDER = """#!/bin/sh
playlist=""
for arg in "$@"; do
  case "$arg" in *.m3u8) playlist="$arg" ;; esac
done
printf '%s\\n' "$@" > "$(dirname "$playlist")/args.txt"
echo '#EXTM3U' > "$playlist"
sleep 30
"""


def hls_arguments(tmp: pathlib.Path) -> list[str]:
    """The ffmpeg arguments hls.py really builds, captured by a stand-in."""
    recorder = tmp / "recorder"
    recorder.write_text(RECORDER)
    recorder.chmod(0o755)

    async def go() -> list[str]:
        config = {"ffmpeg": str(recorder), "hls_dir": str(tmp / "hls"), "hls_start_timeout": 3}
        session = HlsSession("cam", HlsManager(_Broker(), config, tmp))
        await session.start()
        await session.wait_ready()
        args = (session.directory / "args.txt").read_text().splitlines()
        await session.stop()
        return args

    return asyncio.run(go())


def run_hls(args: list[str], source: pathlib.Path, workdir: pathlib.Path) -> subprocess.CompletedProcess:
    """Run those arguments against a file, with every output redirected into ``workdir``."""
    command = [shutil.which("ffmpeg") or "ffmpeg"]
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-i":
            # A few seconds is plenty, and keeps the run short.
            command += ["-t", "4", "-i", str(source)]
            index += 2
            continue
        if arg.endswith((".m3u8", ".ts", ".part")):
            arg = str(workdir / pathlib.Path(arg).name)
        command.append(arg)
        index += 1
    return subprocess.run(command, capture_output=True, text=True, timeout=120, cwd=workdir)


def test_real_ffmpeg() -> None:
    print("real ffmpeg")
    sample = ffmpeg_sample()
    if sample is None:
        print("  SKIP  ffmpeg with libx264 and aac is not installed")
        return
    # The sample's own PMT lists both streams; remove the audio packets and the
    # announcement stays, which is exactly what the camera in #80 sends.
    first_pmt = next(p for p in packets(sample) if pid_of(p) == PMT_PID and p[1] & 0x40)
    audio_pid = next(iter(tsguard.parse_pmt(first_pmt[4:]).audio_pids))
    silent = b"".join(p for p in packets(sample) if pid_of(p) != audio_pid)
    outputs, guard, _clock = run_guard(silent, chunk=PACKET * 7, rate=len(silent) // 4)
    guarded = b"".join(outputs)
    check(guard.mode == "strip" and len(guarded) == len(silent), "the real stream is recognised and only its PMT changes")

    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        (tmp / "silent.ts").write_bytes(silent)
        (tmp / "guarded.ts").write_bytes(guarded)

        def streams_of(path: pathlib.Path) -> list[str]:
            result = subprocess.run(
                ["ffprobe", "-hide_banner", "-v", "quiet", "-show_streams", "-of", "json", str(path)],
                capture_output=True, text=True, timeout=60,
            )
            return [item["codec_type"] for item in json.loads(result.stdout)["streams"]]

        check(streams_of(tmp / "guarded.ts") == ["video"], "ffprobe sees one video stream and no phantom audio")
        print(f"        announced as shipped: {streams_of(tmp / 'silent.ts')}")

        args = hls_arguments(tmp)
        for label, source in (("as the camera sent it", "silent.ts"), ("after the guard", "guarded.ts")):
            work = tmp / label.replace(" ", "_")
            work.mkdir()
            result = run_hls(args, tmp / source, work)
            made = sorted(p.name for p in work.glob("*.ts"))
            print(f"        hls.py's command {label}: exit {result.returncode}, {len(made)} .ts files")
            if label == "after the guard":
                check(result.returncode == 0 and (work / "index.m3u8").exists() and bool(made), "the command hls.py builds now writes HLS from it")
                mp4 = work / "save.mp4"
                cache = next((p for p in work.glob("*.ts.part")), None) or next((p for p in work.glob("*.ts") if not p.name.startswith("segment_")), None)
                if cache is not None:
                    done = subprocess.run(
                        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(cache), "-c", "copy", str(mp4)],
                        capture_output=True, text=True, timeout=60,
                    )
                    check(done.returncode == 0 and mp4.exists(), "and the cached copy converts to MP4 for Save MP4")


def main() -> int:
    test_crc()
    test_healthy_passes_untouched()
    test_no_announcement_passes()
    test_announced_but_never_sent()
    test_audio_on_the_pcr_pid()
    test_chunking_does_not_matter()
    test_leaves_alone_what_it_cannot_read()
    test_switch_and_bounds()
    test_wiring()
    test_real_ffmpeg()
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
