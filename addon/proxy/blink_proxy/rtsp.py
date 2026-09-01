"""RTSP transport for Blink cameras that are not handed an ``immis://`` URL.

Blink does not give every camera the same transport. Measured 2026-08-30 on a
ten-camera account:

    innenraum              owl        immis://
    gartenture_kapfenberg  lotus      immis://
    garten_hinterm_haus    xt         rtsps://

``BlinkStreamBroker.start_liveview`` only accepted ``immis://`` and rejected
everything else outright, so those cameras could never stream.

Which cameras get RTSP
----------------------
This first read the ``blinkRTSP=true`` parameter as a sign of a rollout, so
more cameras over time. That was guessed from a parameter name. Across two
accounts the split by camera type looks like this:

    xt        rtsps      both accounts
    white     rtsps      reviewer's account
    owl       immis      this account
    lotus     immis      this account
    xt2       immis      reviewer's account
    superior  immis      reviewer's account
    catalina  immis      reviewer's account (an Outdoor 3, added to check)

Only the oldest generations get RTSP; everything newer gets ``immis://``,
including the battery-powered outdoor ``catalina`` and ``xt2``. So RTSP reads
as the legacy path rather than the incoming one, and the affected set is
probably fixed rather than growing. That does not make this less useful:
those cameras have no live view at all today.

Why not just hand the URL to ffmpeg
-----------------------------------
Blink's RTSP server violates RFC 2326: it answers **every** request with
``CSeq: 1`` instead of echoing the sequence number of the request.

    OPTIONS   ->  RTSP/1.0 200 OK   CSeq: 1
    DESCRIBE  ->  RTSP/1.0 200 OK   CSeq: 1     <- should be 2

ffmpeg validates CSeq strictly and aborts with ``CSeq 2 expected, 1 received``.
The session dies before Blink is ever asked to wake the camera -- no click, no
LED. So the handshake is done by hand here and the response CSeq is
deliberately **not** checked.

Two more deviations, both survived only because this client treats the fields
as optional:

* ``SETUP`` replies without a ``Session`` header.
* ``SETUP`` replies without a ``Transport`` header -- the server never confirms
  which transport it picked. Interleaved TCP works anyway; it simply starts
  sending.

Interleaving
------------
With RTSP over TCP, control and data share one connection: a ``$`` byte
introduces a data frame (channel, length, payload). Blink starts sending
immediately after ``SETUP``, sometimes *during* the remaining handshake. A
reader that blindly scans for the next ``\\r\\n\\r\\n`` swallows video data and
loses framing -- the first draft of this module died on exactly that. One
shared reader therefore returns either a frame or a response, and the command
helper forwards frames while it waits for its reply.

Output
------
The SDP announces ``a=rtpmap:33 MP2T/90000``, i.e. MPEG-2 TS carried in RTP.
Stripping the RTP header leaves precisely the byte stream that
``/cameras/<slug>/mpegts`` already serves. This class therefore exposes the
same surface as the ``immis://`` stream -- a local TCP server with ``.socket``
and ``.server``, plus a ``feed()`` coroutine -- so ``LiveViewHandle`` and
``mpegts_handler`` needed no changes at all.

Push-to-talk is not available over this transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import ssl
from typing import Any
from urllib.parse import urlparse

from .constants import LOGGER_NAME

LOGGER = logging.getLogger(LOGGER_NAME)

RTP_MIN_HEADER_BYTES = 12
RTSP_HEADER_TERMINATOR = b"\r\n\r\n"
INTERLEAVED_MARKER = b"$"
VIDEO_CHANNEL = 0
# Backlog at which a consumer is dropped. At Blink's bitrate four
# megabytes is roughly two seconds of video: long enough to ride out a
# hiccup, short enough that memory does not run away.
MAX_CONSUMER_BACKLOG_BYTES = 4 * 1024 * 1024
RTSP_TIMEOUT_SECONDS = 20
USER_AGENT = "blink-liveview-proxy"


class RtspError(RuntimeError):
    """The RTSP handshake failed."""


class BlinkRtspLiveStream:
    """Pulls a Blink live view over RTSP and republishes it as MPEG-TS."""

    def __init__(self, camera: Any, response: dict[str, Any]) -> None:
        self.camera = camera
        self.response = response
        self.server_url = str(response.get("server", ""))
        self.server: asyncio.AbstractServer | None = None
        self.socket: socket.socket | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._consumers: list[asyncio.StreamWriter] = []
        self._cseq = 0
        self._session: str | None = None
        self._running = False
        self._packets = 0
        self._bytes_out = 0

    # ------------------------------------------------------------------ setup
    async def start(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        """Open the local TCP server that ``mpegts_handler`` connects to."""
        self.server = await asyncio.start_server(self._add_consumer, host, port or 0)
        self.socket = self.server.sockets[0]
        LOGGER.info("RTSP: local endpoint on %s:%s",
                    self.socket.getsockname()[0], self.socket.getsockname()[1])

    async def _add_consumer(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        self._consumers.append(writer)
        try:
            await reader.read()          # consumers never send anything
        except Exception:
            pass
        finally:
            with contextlib.suppress(ValueError):
                self._consumers.remove(writer)

    # -------------------------------------------------- reading: frame or reply
    async def _frame_or_response(self) -> tuple[str, int, bytes]:
        """Read exactly one unit from the connection.

        A ``$`` byte introduces an interleaved data frame; anything else is the
        start of an RTSP response.
        """
        assert self._reader is not None
        first = await self._reader.readexactly(1)
        if first == INTERLEAVED_MARKER:
            channel = (await self._reader.readexactly(1))[0]
            length = int.from_bytes(await self._reader.readexactly(2), "big")
            return "frame", channel, await self._reader.readexactly(length)
        rest = await self._reader.readuntil(RTSP_HEADER_TERMINATOR)
        return "response", 0, first + rest

    async def _await_response(self) -> bytes:
        """Wait for the next RTSP response, forwarding any frames in between."""
        while True:
            kind, channel, data = await self._frame_or_response()
            if kind == "response":
                return data
            self._dispatch_frame(channel, data)

    def _dispatch_frame(self, channel: int, packet: bytes) -> None:
        """Channel 0 carries video; channel 1 is RTCP and is ignored."""
        if channel != VIDEO_CHANNEL:
            return
        payload = self._rtp_payload(packet)
        if not payload:
            return
        self._packets += 1
        self._bytes_out += len(payload)
        if self._packets == 1:
            LOGGER.info("RTSP: first video packet received")
        for writer in list(self._consumers):
            if writer.is_closing():
                continue
            # Never await drain() here. This module has a single reader that
            # carries control *and* data on one connection, so draining would
            # stall the loop that also reads RTSP responses: Blink would keep
            # sending and the backlog would simply move upstream. For live
            # video the right answer is to drop the slow consumer.
            transport = writer.transport
            if transport is not None:
                backlog = transport.get_write_buffer_size()
                if backlog > MAX_CONSUMER_BACKLOG_BYTES:
                    LOGGER.warning(
                        "RTSP: consumer is %d bytes behind, dropping it",
                        backlog)
                    with contextlib.suppress(ValueError):
                        self._consumers.remove(writer)
                    with contextlib.suppress(Exception):
                        writer.close()
                    continue
            try:
                writer.write(payload)
            except Exception:
                with contextlib.suppress(ValueError):
                    self._consumers.remove(writer)

    # -------------------------------------------------------------- handshake
    async def _request(self, method: str, url: str,
                       headers: dict[str, str] | None = None
                       ) -> tuple[int, dict[str, str], bytes]:
        assert self._writer is not None
        self._cseq += 1
        lines = [f"{method} {url} RTSP/1.0",
                 f"CSeq: {self._cseq}",
                 f"User-Agent: {USER_AGENT}"]
        if self._session:
            lines.append(f"Session: {self._session}")
        for key, value in (headers or {}).items():
            lines.append(f"{key}: {value}")
        self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await self._writer.drain()
        LOGGER.debug("RTSP > %s", method)

        raw = await asyncio.wait_for(self._await_response(),
                                     timeout=RTSP_TIMEOUT_SECONDS)
        text = raw.decode("utf-8", "replace")
        status_line = text.splitlines()[0] if text.splitlines() else ""
        try:
            status = int(status_line.split()[1])
        except (IndexError, ValueError) as error:
            raise RtspError(f"Unparsable RTSP response: {status_line!r}") from error

        headers_out: dict[str, str] = {}
        for line in text.splitlines()[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                headers_out[name.strip().lower()] = value.strip()

        # The response CSeq is deliberately not validated -- see module docstring.
        length = int(headers_out.get("content-length", "0") or 0)
        body = b""
        if length:
            assert self._reader is not None
            body = await asyncio.wait_for(self._reader.readexactly(length),
                                          timeout=RTSP_TIMEOUT_SECONDS)
        LOGGER.debug("RTSP < %s", status)
        return status, headers_out, body

    async def connect(self) -> None:
        """Run OPTIONS, DESCRIBE, SETUP and PLAY."""
        parts = urlparse(self.server_url)
        host = parts.hostname or ""
        port = parts.port or 443
        if not host:
            raise RtspError(f"No host in {self.server_url!r}")

        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(host, port,
                                        ssl=ssl.create_default_context(),
                                        server_hostname=host),
                timeout=RTSP_TIMEOUT_SECONDS)
        except ssl.SSLError as error:
            LOGGER.warning("RTSP: TLS verification failed (%s), retrying unverified",
                           error)
            relaxed = ssl.create_default_context()
            relaxed.check_hostname = False
            relaxed.verify_mode = ssl.CERT_NONE
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=relaxed,
                                        server_hostname=host),
                timeout=RTSP_TIMEOUT_SECONDS)

        status, _, _ = await self._request("OPTIONS", self.server_url)
        if status != 200:
            raise RtspError(f"OPTIONS returned {status}")

        status, _, sdp = await self._request("DESCRIBE", self.server_url,
                                             {"Accept": "application/sdp"})
        if status != 200:
            raise RtspError(f"DESCRIBE returned {status}")
        LOGGER.info("RTSP: SDP received, %d bytes", len(sdp))
        LOGGER.debug("SDP:\n%s", sdp.decode("utf-8", "replace"))

        track = self._track_from_sdp(sdp)

        # Interleaved TCP keeps everything on one connection, which avoids
        # needing a UDP return path -- important inside a container.
        status, headers, _ = await self._request(
            "SETUP", track, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
        if status != 200:
            raise RtspError(f"SETUP returned {status} (interleaved TCP)")
        self._session = headers.get("session", "").split(";")[0].strip() or None
        LOGGER.info("RTSP: session %s, transport %s",
                    self._session, headers.get("transport"))

        status, _, _ = await self._request("PLAY", self.server_url)
        if status != 200:
            raise RtspError(f"PLAY returned {status}")
        LOGGER.info("RTSP: PLAY accepted, waiting for video")
        self._running = True

    def _track_from_sdp(self, sdp: bytes) -> str:
        """Return the video track control URL, falling back to the base URL."""
        in_video = False
        for line in sdp.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line.startswith("m="):
                in_video = line.startswith("m=video")
            elif in_video and line.startswith("a=control:"):
                value = line.split(":", 1)[1].strip()
                if value in ("*", ""):
                    return self.server_url
                if value.startswith(("rtsp://", "rtsps://")):
                    return value
                separator = "" if self.server_url.endswith("/") else "/"
                return self.server_url + separator + value
        LOGGER.info("RTSP: no a=control in SDP, using the base URL")
        return self.server_url

    # ------------------------------------------------------------- data path
    async def feed(self) -> None:
        """Run until the connection ends, forwarding frames to consumers."""
        try:
            while self._running:
                kind, channel, data = await self._frame_or_response()
                if kind == "frame":
                    self._dispatch_frame(channel, data)
                else:
                    LOGGER.debug("RTSP: interim response (%d bytes)", len(data))
        except (asyncio.IncompleteReadError, ConnectionResetError):
            LOGGER.info("RTSP: connection closed after %d packets, %d bytes",
                        self._packets, self._bytes_out)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.error("RTSP: data path aborted: %s", error)

    @staticmethod
    def _rtp_payload(packet: bytes) -> bytes:
        """Strip the RTP header. Payload type 33 is MP2T, i.e. ready-made TS."""
        if len(packet) < RTP_MIN_HEADER_BYTES:
            return b""
        first = packet[0]
        csrc_count = first & 0x0F
        has_extension = bool(first & 0x10)
        offset = RTP_MIN_HEADER_BYTES + 4 * csrc_count
        if has_extension:
            if len(packet) < offset + 4:
                return b""
            words = int.from_bytes(packet[offset + 2:offset + 4], "big")
            offset += 4 + 4 * words
        return packet[offset:] if offset < len(packet) else b""

    # ---------------------------------------------------------------- teardown
    def stop(self) -> None:
        self._running = False
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
        for writer in list(self._consumers):
            with contextlib.suppress(Exception):
                writer.close()
        self._consumers.clear()
        if self.server is not None:
            self.server.close()

    # Push-to-talk has no equivalent over RTSP.
    async def send_session_command(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError("push-to-talk is not available over RTSP")

    async def send_audio_config(self) -> None:
        raise NotImplementedError("push-to-talk is not available over RTSP")

    async def send_audio_frame(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError("push-to-talk is not available over RTSP")
