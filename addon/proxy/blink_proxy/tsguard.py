"""Keep a live MPEG-TS from promising audio the camera never sends.

A Blink camera announces its streams in the transport stream's PMT, and some
cameras announce an audio stream they then send nothing on (a microphone that is
switched off is the likely reason; nothing here depends on the reason). Every
consumer of the stream reads that announcement as a promise, and they break it
differently:

* mpegts.js, which plays the dashboard's live-view dialog on desktop and
  Android, will not release a single frame until it has the first frame of
  *every* announced track. Video arrives, is buffered, and is never shown: the
  dialog sits on "Waking camera and waiting for video" until the session limit
  ends it. Nothing is logged, because nothing failed.
* ffmpeg, which cuts the HLS an iPhone plays and the MP4 that Save MP4 hands
  back, cannot write an audio stream whose sample rate it never learned. It
  stops with ``sample rate not set ... Could not write header`` and the live
  view fails outright.
* Home Assistant's own stream (the more-info dialog) shrugs it off, which is
  what makes this look like a fault in one dialog rather than in the camera.

So the camera's own stream is made honest once, here, before it reaches any of
them. :class:`AudioGuard` watches the start of the stream and, if the PMT
announces audio and none arrives within ``wait_seconds``, drops the audio from
the PMT for the rest of the session and discards any audio packets, which turns
it into the video-only stream every consumer already handles. A stream whose
audio does arrive is passed through byte for byte, and so is one that announces
none.

The decision is made once and never revisited. A player cannot grow a track
halfway through a session (Chrome refuses a source buffer added after
playback has started), so a late audio stream is dropped rather than
announced in the middle of the picture.

Only the PMT is ever rewritten, and only when it fits one TS packet, which a
camera's two- or three-stream PMT always does. Anything the guard cannot parse
or rewrite safely is passed through untouched: the guard exists to unblock
streams, never to alter a working one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .constants import LOGGER_NAME

LOGGER = logging.getLogger(LOGGER_NAME)

TS_PACKET_BYTES = 188
SYNC_BYTE = 0x47
PAT_PID = 0x0000
NULL_PID = 0x1FFF
TABLE_ID_PAT = 0x00
TABLE_ID_PMT = 0x02

# Stream types a consumer will wait on as audio: MPEG-1 and MPEG-2 audio, AAC in
# ADTS and in LATM, AC-3 and E-AC-3. Private data (0x06) is deliberately absent:
# it is audio only with a descriptor, and the guard must not guess.
AUDIO_STREAM_TYPES = frozenset({0x03, 0x04, 0x0F, 0x11, 0x81, 0x87})

# Healthy Blink streams carry audio from the first packets: ffmpeg's half second
# of analysis (ffmpeg_analyzeduration) already depends on it. Two seconds is four
# times that, and is the longest a camera with no audio can delay its own video.
DEFAULT_AUDIO_WAIT_SECONDS = 2.0

# What may be held back while deciding. A few seconds of Blink video is a
# fraction of this; it only bounds memory if the clock never gets to speak.
MAX_HELD_BYTES = 4 * 1024 * 1024


def audio_wait_from_config(config: dict[str, Any]) -> float:
    """``liveview_audio_wait_seconds``, or the default if it is not a number.

    Zero or less turns the guard off, which is the way back to the camera's
    stream exactly as it sent it.
    """
    value = config.get("liveview_audio_wait_seconds", DEFAULT_AUDIO_WAIT_SECONDS)
    try:
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_AUDIO_WAIT_SECONDS


def _crc_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _crc_table()


def mpeg_crc32(data: bytes) -> int:
    """The CRC-32/MPEG-2 that ends every PSI section."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[(crc >> 24) ^ byte]
    return crc


def _packet_pid(packet: bytes | bytearray) -> int:
    return ((packet[1] & 0x1F) << 8) | packet[2]


def _payload_of(packet: bytes | bytearray) -> bytes:
    """The payload of one TS packet, or b"" if it carries none."""
    control = (packet[3] >> 4) & 0x3
    if not control & 0x1:
        return b""
    offset = 4
    if control & 0x2:
        offset += 1 + packet[4]
    return bytes(packet[offset:]) if offset < TS_PACKET_BYTES else b""


def _section(payload: bytes) -> bytes | None:
    """The PSI section that starts in this payload, if it ends in it too."""
    if not payload:
        return None
    start = 1 + payload[0]
    if start + 3 > len(payload):
        return None
    length = ((payload[start + 1] & 0x0F) << 8) | payload[start + 2]
    if start + 3 + length > len(payload):
        return None
    return payload[start : start + 3 + length]


def parse_pat(payload: bytes) -> int | None:
    """The PID of the first program's PMT, from a PAT packet's payload."""
    section = _section(payload)
    if section is None or section[0] != TABLE_ID_PAT:
        return None
    end = len(section) - 4
    position = 8
    while position + 4 <= end:
        program = (section[position] << 8) | section[position + 1]
        if program != 0:
            return ((section[position + 2] & 0x1F) << 8) | section[position + 3]
        position += 4
    return None


@dataclass(frozen=True)
class Pmt:
    """A PMT section, taken apart far enough to put it back together."""

    section: bytes
    pcr_pid: int
    program_info_end: int
    # (stream_type, elementary_PID, the whole entry including its descriptors)
    streams: tuple[tuple[int, int, bytes], ...]

    @property
    def audio_pids(self) -> dict[int, int]:
        return {
            pid: stream_type
            for stream_type, pid, _entry in self.streams
            if stream_type in AUDIO_STREAM_TYPES
        }


def parse_pmt(payload: bytes) -> Pmt | None:
    """Parse the PMT that starts and ends in this packet's payload."""
    section = _section(payload)
    if section is None or section[0] != TABLE_ID_PMT or len(section) < 16:
        return None
    end = len(section) - 4
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    program_info_end = 12 + (((section[10] & 0x0F) << 8) | section[11])
    if program_info_end > end:
        return None
    streams: list[tuple[int, int, bytes]] = []
    position = program_info_end
    while position + 5 <= end:
        info_length = ((section[position + 3] & 0x0F) << 8) | section[position + 4]
        entry_end = position + 5 + info_length
        if entry_end > end:
            return None
        pid = ((section[position + 1] & 0x1F) << 8) | section[position + 2]
        streams.append((section[position], pid, section[position:entry_end]))
        position = entry_end
    if position != end:
        return None
    return Pmt(section, pcr_pid, program_info_end, tuple(streams))


def rewrite_pmt_packet(packet: bytes, drop: set[int]) -> bytes | None:
    """This PMT packet with the elementary streams in ``drop`` taken out.

    None when the packet is not one the guard can rebuild exactly, in which case
    the caller passes the original through.
    """
    if (packet[3] >> 4) & 0x3 != 0x1:  # an adaptation field: not a plain PMT
        return None
    pmt = parse_pmt(_payload_of(packet))
    if pmt is None:
        return None
    kept = [(pid, entry) for _type, pid, entry in pmt.streams if pid not in drop]
    pcr_pid = pmt.pcr_pid
    if pcr_pid in drop:
        pcr_pid = kept[0][0] if kept else NULL_PID
    section = pmt.section
    body = bytearray(section[3:8])
    body += bytes([0xE0 | (pcr_pid >> 8), pcr_pid & 0xFF])
    body += section[10 : pmt.program_info_end]
    for _pid, entry in kept:
        body += entry
    length = len(body) + 4  # the CRC that follows
    head = bytes([section[0], (section[1] & 0xF0) | (length >> 8), length & 0xFF])
    without_crc = head + bytes(body)
    rebuilt = without_crc + mpeg_crc32(without_crc).to_bytes(4, "big")
    payload = b"\x00" + rebuilt
    if len(payload) > TS_PACKET_BYTES - 4:
        return None
    header = bytes([packet[0], packet[1] | 0x40, packet[2], (packet[3] & 0x0F) | 0x10])
    return header + payload.ljust(TS_PACKET_BYTES - 4, b"\xff")


class AudioGuard:
    """Make a live TS stream's audio announcement match the audio it sends.

    Feed it each chunk as it arrives from the camera and send on whatever
    :meth:`push` returns. It returns the chunk itself, untouched, once the stream
    is known to be fine, and nothing at all while it is still finding out.
    """

    def __init__(
        self,
        label: str = "",
        wait_seconds: float = DEFAULT_AUDIO_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.label = label
        self.wait_seconds = wait_seconds
        self._clock = clock
        # "deciding" holds the start of the stream, "pass" forwards it as it is,
        # "strip" forwards it without the audio. A wait of zero turns it off.
        self.mode = "deciding" if wait_seconds > 0 else "pass"
        self._held = bytearray()
        self._scanned = 0
        self._carry = bytearray()
        self._started_at: float | None = None
        self._pmt_at: float | None = None
        self._pmt_pid: int | None = None
        self._audio_pids: dict[int, int] = {}
        self._audio_seen = False
        self._unusable = False

    # ------------------------------------------------------------- interface
    def push(self, data: bytes) -> bytes:
        """Take a chunk from the camera; return the bytes to send on now."""
        if self.mode == "pass":
            return data
        if self.mode == "strip":
            return self._strip(data)
        if self._started_at is None:
            self._started_at = self._clock()
        self._held += data
        self._scan()
        decision = self._decide()
        if decision is None:
            return b""
        return self._release(decision)

    def flush(self) -> bytes:
        """The stream has ended: hand back anything still held, unmodified."""
        if self.mode == "deciding":
            return self._release("pass")
        self._carry.clear()
        return b""

    # --------------------------------------------------------------- deciding
    def _scan(self) -> None:
        """Look at the whole packets held since the last scan."""
        held = self._held
        while len(held) - self._scanned >= TS_PACKET_BYTES:
            position = self._scanned
            if held[position] != SYNC_BYTE:
                self._scanned += 1
                continue
            packet = held[position : position + TS_PACKET_BYTES]
            self._scanned += TS_PACKET_BYTES
            pid = _packet_pid(packet)
            starts = bool(packet[1] & 0x40)
            if pid == PAT_PID and starts and self._pmt_pid is None:
                self._pmt_pid = parse_pat(_payload_of(packet))
            elif pid == self._pmt_pid and starts:
                self._note_pmt(packet)
            elif pid in self._audio_pids and _payload_of(packet):
                self._audio_seen = True

    def _note_pmt(self, packet: bytes | bytearray) -> None:
        pmt = parse_pmt(_payload_of(packet))
        if pmt is None:
            # Longer than one packet, or damaged. Either way there is nothing
            # the guard can safely do, so it stops getting in the way.
            self._unusable = True
            return
        if self._pmt_at is None:
            self._pmt_at = self._clock()
        self._audio_pids = pmt.audio_pids

    def _decide(self) -> str | None:
        if self._audio_seen or self._unusable:
            return "pass"
        now = self._clock()
        if self._pmt_at is None:
            if now - (self._started_at or now) >= self.wait_seconds:
                return "pass"  # no PMT to correct
        elif not self._audio_pids:
            return "pass"  # nothing promised, nothing to break
        elif now - self._pmt_at >= self.wait_seconds:
            return "strip"
        if len(self._held) >= MAX_HELD_BYTES:
            return "pass"
        return None

    def _release(self, decision: str) -> bytes:
        held = bytes(self._held)
        self._held.clear()
        self._scanned = 0
        self.mode = decision
        if decision == "pass":
            return held
        LOGGER.info(
            "Camera %s announced audio (PID %s) but sent none for %.1fs; "
            "serving video only so players and ffmpeg do not wait for it. "
            "Check the camera's audio setting in the Blink app if sound is "
            "expected.",
            self.label or "(unnamed)",
            ", ".join(f"0x{pid:x}" for pid in sorted(self._audio_pids)),
            self.wait_seconds,
        )
        return self._strip(held)

    # ------------------------------------------------------------- stripping
    def _strip(self, data: bytes) -> bytes:
        """The packets in ``data`` without the audio stream or its mention."""
        buffer = self._carry + data
        drop = set(self._audio_pids)
        out = bytearray()
        position = 0
        while len(buffer) - position >= TS_PACKET_BYTES:
            if buffer[position] != SYNC_BYTE:
                position += 1
                continue
            packet = bytes(buffer[position : position + TS_PACKET_BYTES])
            position += TS_PACKET_BYTES
            pid = _packet_pid(packet)
            if pid in drop:
                continue
            if pid == self._pmt_pid and packet[1] & 0x40:
                packet = rewrite_pmt_packet(packet, drop) or packet
            out += packet
        self._carry = bytearray(buffer[position:])
        return bytes(out)
