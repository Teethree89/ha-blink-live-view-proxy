"""Experimental push-to-talk WebSocket bridge."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from aiohttp import WSMsgType, web

from .auth import check_authorized
from .blink import LiveViewHandle, camera_ptt_supported
from .constants import (
    AAC_FRAME_SAMPLES,
    AUDIO_CLOCK_RATE,
    LOGGER_NAME,
    PTT_TARGET_SAMPLE_RATE,
)

LOGGER = logging.getLogger(LOGGER_NAME)

def liveview_session_key(slug: str, session: str) -> str:
    """Return the active live-view registry key for a browser session."""
    return f"{slug}:{session}"

class PttAudioBridge:
    """Encode browser microphone PCM and forward it as IMMI audio frames."""

    def __init__(
        self,
        app: web.Application,
        slug: str,
        session: str,
        status_callback: Any | None = None,
    ) -> None:
        self.app = app
        self.slug = slug
        self.session = session
        self.status_callback = status_callback
        self.process: asyncio.subprocess.Process | None = None
        self.stdout_task: asyncio.Task[None] | None = None
        self.timestamp = 0
        self.started = False
        self.listening_notified = False
        self.audio_frames_sent = 0
        self.audio_bytes_sent = 0

    def _liveview(self) -> LiveViewHandle:
        liveview = self.app["active_liveviews"].get(
            liveview_session_key(self.slug, self.session)
        )
        if liveview is None:
            raise RuntimeError("No active live view for this microphone session")
        return liveview

    async def start(self, sample_rate: int) -> None:
        """Start a push-to-talk encoder and notify Blink."""
        await self.stop(send_stop=False)
        sample_rate = max(8000, min(96000, int(sample_rate or 48000)))
        liveview = self._liveview()
        await liveview.start_audio()

        config = self.app["config"]
        self.process = await asyncio.create_subprocess_exec(
            str(config["ffmpeg"]),
            "-hide_banner",
            "-loglevel",
            str(config.get("ffmpeg_loglevel", "warning")),
            "-fflags",
            "nobuffer",
            "-f",
            "s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-ac",
            "1",
            "-ar",
            str(PTT_TARGET_SAMPLE_RATE),
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            str(config.get("ptt_aac_bitrate", "40k")),
            "-f",
            "adts",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.timestamp = 0
        self.audio_frames_sent = 0
        self.audio_bytes_sent = 0
        self.listening_notified = False
        self.started = True
        self.stdout_task = asyncio.create_task(
            self._forward_adts_frames(), name=f"blink-ptt-{self.slug}"
        )
        LOGGER.info(
            "Started push-to-talk bridge for %s (%s AAC payloads)",
            self.slug,
            "raw" if self.app["config"].get("ptt_strip_adts", True) else "ADTS",
        )

    async def write_pcm(self, data: bytes) -> None:
        """Write signed 16-bit mono PCM into ffmpeg."""
        if not data or self.process is None or self.process.stdin is None:
            return
        if self.process.returncode is not None:
            raise RuntimeError("push-to-talk encoder exited")
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def stop(self, *, send_stop: bool = True) -> None:
        """Stop encoding and optionally send Blink's stop-audio command."""
        process = self.process
        self.process = None

        if process is not None:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
                with contextlib.suppress(Exception):
                    await process.stdin.wait_closed()
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.terminate()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(process.wait(), timeout=2)
                    if process.returncode is None:
                        process.kill()
                        await process.wait()

        if self.stdout_task is not None:
            self.stdout_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError, OSError):
                await self.stdout_task
            self.stdout_task = None

        if send_stop and self.started:
            with contextlib.suppress(Exception):
                await self._liveview().stop_audio()
            LOGGER.info(
                "Stopped push-to-talk bridge for %s after %d AAC frames (%d bytes)",
                self.slug,
                self.audio_frames_sent,
                self.audio_bytes_sent,
            )
        self.started = False
        self.listening_notified = False

    async def _notify(self, payload: dict[str, Any]) -> None:
        if self.status_callback is not None:
            await self.status_callback(payload)

    async def _forward_adts_frames(self) -> None:
        """Read ffmpeg ADTS frames and write IMMI audio packets."""
        process = self.process
        if process is None or process.stdout is None:
            return
        stdout = process.stdout

        strip_adts = bool(self.app["config"].get("ptt_strip_adts", False))
        frame_ticks = int(AAC_FRAME_SAMPLES * AUDIO_CLOCK_RATE / PTT_TARGET_SAMPLE_RATE)

        while True:
            try:
                header = await stdout.readexactly(7)
            except asyncio.IncompleteReadError:
                return

            if len(header) < 7 or header[0] != 0xFF or (header[1] & 0xF0) != 0xF0:
                LOGGER.warning("Invalid ADTS header from push-to-talk encoder")
                return

            frame_length = (
                ((header[3] & 0x03) << 11) | (header[4] << 3) | (header[5] >> 5)
            )
            protection_absent = header[1] & 0x01
            header_length = 7 if protection_absent else 9
            if frame_length < header_length:
                LOGGER.warning("Invalid ADTS frame length: %d", frame_length)
                return

            try:
                rest = await stdout.readexactly(frame_length - 7)
            except asyncio.IncompleteReadError:
                return
            frame = header + rest
            payload = frame[header_length:] if strip_adts else frame
            if payload:
                await self._liveview().send_audio_frame(self.timestamp, payload)
                self.audio_frames_sent += 1
                self.audio_bytes_sent += len(payload)
                if not self.listening_notified:
                    self.listening_notified = True
                    await self._notify(
                        {
                            "type": "listening",
                            "frames": self.audio_frames_sent,
                        }
                    )
                self.timestamp = (self.timestamp + frame_ticks) & 0xFFFFFFFF

async def ptt_handler(request: web.Request) -> web.WebSocketResponse:
    """Handle experimental push-to-talk audio for an active live-view session."""
    check_authorized(request)
    slug = request.match_info["slug"]
    try:
        camera = request.app["client"].camera_for_slug(slug)
    except KeyError as err:
        raise web.HTTPNotFound(text=f"Unknown camera slug: {slug}\n") from err
    if not camera_ptt_supported(camera, request.app["config"]):
        raise web.HTTPBadRequest(
            text=f"Push-to-talk is not enabled for camera type {camera.camera_type or camera.product_type or 'unknown'}\n"
        )
    session = request.query.get("session", "")
    websocket = web.WebSocketResponse(heartbeat=20, max_msg_size=1024 * 1024)
    await websocket.prepare(request)

    async def send_status(payload: dict[str, Any]) -> None:
        if websocket.closed:
            return
        try:
            await websocket.send_json(payload)
        except (ConnectionResetError, RuntimeError):
            LOGGER.debug("Push-to-talk websocket already closing for %s", slug)

    if not session:
        await send_status({"type": "error", "message": "Missing session"})
        await websocket.close()
        return websocket

    bridge = PttAudioBridge(request.app, slug, session, send_status)
    try:
        async for message in websocket:
            if message.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                    message_type = payload.get("type")
                    if message_type == "start":
                        await bridge.start(int(payload.get("sampleRate") or 48000))
                        await send_status({"type": "started"})
                    elif message_type == "stop":
                        await bridge.stop()
                        await send_status({"type": "stopped"})
                    else:
                        await send_status(
                            {"type": "error", "message": "Unknown command"}
                        )
                except Exception as err:  # noqa: BLE001
                    LOGGER.exception("Push-to-talk command failed for %s", slug)
                    message = str(err)
                    if "IMMI target is not connected" in message:
                        message = (
                            "Camera audio channel is no longer connected. "
                            "Start live view again and hold Talk while video is playing."
                        )
                    await send_status({"type": "error", "message": message})
            elif message.type == WSMsgType.BINARY:
                try:
                    await bridge.write_pcm(message.data)
                except Exception as err:  # noqa: BLE001
                    LOGGER.exception("Push-to-talk audio write failed for %s", slug)
                    await send_status({"type": "error", "message": str(err)})
                    await websocket.close()
            elif message.type == WSMsgType.ERROR:
                LOGGER.debug("Push-to-talk websocket failed: %s", websocket.exception())
                break
    finally:
        await bridge.stop()
    return websocket
