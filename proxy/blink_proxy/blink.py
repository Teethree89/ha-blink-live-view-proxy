"""BlinkPy client and direct IMMI live-view bridge."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import ssl
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import ClientSession
from blinkpy import api as blink_api
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink
from blinkpy.livestream import BlinkLiveStream

from .config import create_client_session, load_json_file, resolve_path, save_json_file
from .constants import (
    IMMI_AUDIO_CONFIG_SEQUENCE,
    IMMI_DATA_FLAG_AUDIO,
    IMMI_DATA_FLAG_AUDIO_CONFIG,
    IMMI_DATA_FLAG_SESSION_LV_CMD,
    IMMI_HEADER_BYTES,
    LIVEVIEW_SESSION_COMMAND_START_AUDIO,
    LIVEVIEW_SESSION_COMMAND_STOP_AUDIO,
    LOGGER_NAME,
    MAX_IMMI_PAYLOAD_BYTES,
)
from .util import normalize_slug, redact_liveview_response

LOGGER = logging.getLogger(LOGGER_NAME)

def camera_ptt_supported(camera: Any, config: dict[str, Any]) -> bool:
    """Return whether experimental push-to-talk should be offered."""
    camera_type = str(getattr(camera, "camera_type", "") or "").casefold()
    product_type = str(getattr(camera, "product_type", "") or "").casefold()
    disabled_camera_types = {
        str(item).casefold()
        for item in config.get("ptt_disabled_camera_types", [])
    }
    disabled_product_types = {
        str(item).casefold()
        for item in config.get("ptt_disabled_product_types", [])
    }
    return (
        camera_type not in disabled_camera_types
        and product_type not in disabled_product_types
    )

class TokenAwareBlinkLiveStream(BlinkLiveStream):
    """BlinkPy stream with local protocol fixes for Blink IMMI live view."""

    def __init__(self, camera: Any, response: dict[str, Any], send_token: bool):
        super().__init__(camera, response)
        self.liveview_token = response.get("liveview_token", "") if send_token else ""
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _add_fixed_string(buffer: bytearray, value: str | None, length: int) -> None:
        payload = (value or "").encode("utf-8")[:length].ljust(length, b"\x00")
        buffer.extend(len(payload).to_bytes(4, byteorder="big"))
        buffer.extend(payload)

    def get_auth_header(self) -> bytearray:
        auth_header = bytearray([0x00, 0x00, 0x00, 0x28])

        self._add_fixed_string(auth_header, self.camera.serial, 16)

        client_id = urllib.parse.parse_qs(self.target.query).get("client_id", [0])[0]
        auth_header.extend(int(client_id).to_bytes(4, byteorder="big"))
        auth_header.extend([0x01, 0x08])

        self._add_fixed_string(auth_header, self.liveview_token, 64)

        conn_id = self.target.path.split("/")[-1].split("__")[0]
        self._add_fixed_string(auth_header, conn_id, 16)

        auth_header.extend([0x00, 0x00, 0x00, 0x01])
        return auth_header

    async def write_immi_frame(
        self, msgtype: int, sequence: int, payload: bytes = b""
    ) -> None:
        """Write one outbound IMMI frame to Blink."""
        if self.target_writer is None or self.target_writer.is_closing():
            raise RuntimeError("Blink IMMI target is not connected")
        if len(payload) > MAX_IMMI_PAYLOAD_BYTES:
            raise ValueError(f"IMMI payload is too large: {len(payload)} bytes")

        header = bytearray()
        header.append(msgtype & 0xFF)
        header.extend((sequence & 0xFFFFFFFF).to_bytes(4, byteorder="big"))
        header.extend(len(payload).to_bytes(4, byteorder="big"))

        async with self._write_lock:
            self.target_writer.write(header)
            if payload:
                self.target_writer.write(payload)
            await self.target_writer.drain()

    async def send_session_command(self, command: int) -> None:
        """Send a Walnut live-view session command."""
        await self.write_immi_frame(IMMI_DATA_FLAG_SESSION_LV_CMD, command)

    async def send_audio_config(self) -> None:
        """Send the empty AAC-LC audio config marker seen in Blink sessions."""
        await self.write_immi_frame(
            IMMI_DATA_FLAG_AUDIO_CONFIG,
            IMMI_AUDIO_CONFIG_SEQUENCE,
        )

    async def send_audio_frame(self, timestamp: int, payload: bytes) -> None:
        """Send one encoded microphone audio frame."""
        await self.write_immi_frame(IMMI_DATA_FLAG_AUDIO, timestamp, payload)

    async def _read_immi_frame(self) -> tuple[int, int, bytes] | None:
        """Read one complete IMMI frame from Blink's TLS stream."""
        try:
            header = await self.target_reader.readexactly(IMMI_HEADER_BYTES)
        except asyncio.IncompleteReadError as err:
            if err.partial:
                LOGGER.warning(
                    "Blink IMMI stream ended mid-header: %d bytes, expected %d",
                    len(err.partial),
                    IMMI_HEADER_BYTES,
                )
            else:
                LOGGER.debug("Blink IMMI stream ended before the next header")
            return None

        msgtype = header[0]
        sequence = int.from_bytes(header[1:5], byteorder="big")
        payload_length = int.from_bytes(header[5:9], byteorder="big")
        LOGGER.debug(
            "Received IMMI packet: msgtype=%d, sequence=%d, payload_length=%d",
            msgtype,
            sequence,
            payload_length,
        )

        if payload_length > MAX_IMMI_PAYLOAD_BYTES:
            LOGGER.warning(
                "Blink IMMI payload is too large: %d bytes, max %d",
                payload_length,
                MAX_IMMI_PAYLOAD_BYTES,
            )
            return None

        if payload_length == 0:
            return msgtype, sequence, b""

        try:
            payload = await self.target_reader.readexactly(payload_length)
        except asyncio.IncompleteReadError as err:
            LOGGER.warning(
                "Blink IMMI stream ended mid-payload: %d bytes, expected %d",
                len(err.partial),
                payload_length,
            )
            return None

        return msgtype, sequence, payload

    async def recv(self) -> None:
        """Copy complete MPEG-TS payload frames from Blink to local clients."""
        if self.target_reader is None or self.target_writer is None:
            return

        try:
            LOGGER.debug("Starting exact-frame copy from Blink target to clients")
            while not self.target_reader.at_eof():
                frame = await self._read_immi_frame()
                if frame is None:
                    break

                msgtype, _sequence, payload = frame
                if not payload:
                    LOGGER.debug("Skipping empty IMMI payload for msgtype %d", msgtype)
                    continue

                if msgtype != 0x00:
                    LOGGER.debug(
                        "Skipping unsupported IMMI msgtype %d (%d bytes, prefix=%s)",
                        msgtype,
                        len(payload),
                        payload[:16].hex(),
                    )
                    continue

                if payload[0] != 0x47:
                    LOGGER.debug(
                        "Skipping video payload missing MPEG-TS sync byte "
                        "(%d bytes, prefix=%s)",
                        len(payload),
                        payload[:16].hex(),
                    )
                    continue

                LOGGER.debug("Sending %d MPEG-TS bytes to clients", len(payload))
                for writer in list(self.clients):
                    if writer.is_closing():
                        continue
                    writer.write(payload)
                    await writer.drain()

                await asyncio.sleep(0)
        except ssl.SSLError as err:
            if err.reason != "APPLICATION_DATA_AFTER_CLOSE_NOTIFY":
                LOGGER.exception("SSL error while receiving Blink IMMI data")
        except Exception:
            LOGGER.exception("Error while receiving Blink IMMI data")
        finally:
            if self.target_writer is not None and not self.target_writer.is_closing():
                self.target_writer.close()
            LOGGER.debug("Receiving was aborted, aborting sending")

class BlinkClient:
    """Owns BlinkPy auth/session state and camera lookup."""

    def __init__(self, config: dict[str, Any], config_base: Path, pin: str | None):
        self.config = config
        self.config_base = config_base
        self.pin = pin
        self.auth_file = resolve_path(config["auth_file"], config_base)
        self.session: ClientSession | None = None
        self.blink: Blink | None = None

    async def start(self) -> None:
        if self.blink is not None:
            return

        self.session = create_client_session()
        login_data = load_json_file(self.auth_file)

        username = os.getenv(self.config["username_env"], "")
        password = os.getenv(self.config["password_env"], "")
        if username:
            login_data["username"] = username
        if password:
            login_data["password"] = password

        auth: Auth | None = None

        def save_auth_callback() -> None:
            if auth is not None:
                auth_data = dict(auth.login_attributes)
                auth_data.pop("password", None)
                save_json_file(self.auth_file, auth_data)

        auth = Auth(
            login_data=login_data,
            no_prompt=True,
            session=self.session,
            callback=save_auth_callback,
        )
        blink = Blink(refresh_rate=60, session=self.session)
        blink.auth = auth
        self.blink = blink

        try:
            try:
                started = await blink.start()
            except BlinkTwoFARequiredError:
                code = self.pin or os.getenv(self.config["twofa_env"], "")
                if not code and sys.stdin.isatty():
                    code = input("Blink 2FA code: ").strip()
                if not code:
                    raise RuntimeError(
                        "Blink requires 2FA. Set BLINK_2FA_CODE for this run "
                        "or run interactively once so the refresh token can be cached."
                    ) from None
                started = await blink.send_2fa_code(code)

            if not started:
                raise RuntimeError("Blink login/setup failed")
            save_auth_callback()
            LOGGER.info("Blink login ready; discovered %d cameras", len(blink.cameras))
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
        self.session = None
        self.blink = None

    def configured_cameras(self) -> dict[str, dict[str, Any]]:
        return self.config.get("cameras", {})

    def _require_blink(self) -> Blink:
        if self.blink is None:
            raise RuntimeError("Blink client is not started")
        return self.blink

    def camera_for_slug(self, slug: str) -> Any:
        blink = self._require_blink()
        cameras = list(blink.cameras.items())
        spec = self.configured_cameras().get(slug, {})

        for _name, camera in cameras:
            if spec.get("id") and str(camera.camera_id) == str(spec["id"]):
                return camera
            if spec.get("serial") and str(camera.serial) == str(spec["serial"]):
                return camera
            if spec.get("name") and str(camera.name).casefold() == str(
                spec["name"]
            ).casefold():
                return camera

        for name, camera in cameras:
            if normalize_slug(name) == slug:
                return camera

        raise KeyError(slug)

    def camera_slug(self, camera: Any, fallback_name: str) -> str:
        for slug, spec in self.configured_cameras().items():
            if spec.get("id") and str(camera.camera_id) == str(spec["id"]):
                return slug
            if spec.get("serial") and str(camera.serial) == str(spec["serial"]):
                return slug
        return normalize_slug(fallback_name)

    def list_cameras(self) -> list[dict[str, Any]]:
        blink = self._require_blink()
        rows = []
        for name, camera in blink.cameras.items():
            slug = self.camera_slug(camera, name)
            rows.append(
                {
                    "slug": slug,
                    "name": name,
                    "id": str(camera.camera_id),
                    "serial": camera.serial,
                    "network_id": str(camera.network_id),
                    "camera_type": camera.camera_type or "default",
                    "product_type": camera.product_type,
                    "ptt_supported": camera_ptt_supported(camera, self.config),
                    "entity_id": self.configured_cameras()
                    .get(slug, {})
                    .get("entity_id"),
                    "mpegts_url": f"/cameras/{slug}/mpegts",
                    "hls_url": f"/cameras/{slug}/hls/index.m3u8",
                }
            )
        rows.sort(key=lambda row: row["slug"])
        return rows

@dataclass
class LiveViewHandle:
    stream: TokenAwareBlinkLiveStream
    feed_task: asyncio.Task[None]
    config: dict[str, Any]

    @property
    def host(self) -> str:
        return str(self.stream.socket.getsockname()[0])

    @property
    def port(self) -> int:
        return int(self.stream.socket.getsockname()[1])

    @property
    def tcp_url(self) -> str:
        return f"tcp://{self.host}:{self.port}"

    async def close(self) -> None:
        self.stream.stop()
        self.feed_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.feed_task
        server = self.stream.server
        if server is not None:
            await server.wait_closed()

    async def start_audio(self) -> None:
        await self.stream.send_session_command(LIVEVIEW_SESSION_COMMAND_START_AUDIO)
        if bool(self.config.get("ptt_send_audio_config", False)):
            await self.stream.send_audio_config()

    async def stop_audio(self) -> None:
        await self.stream.send_session_command(LIVEVIEW_SESSION_COMMAND_STOP_AUDIO)

    async def send_audio_frame(self, timestamp: int, payload: bytes) -> None:
        await self.stream.send_audio_frame(timestamp, payload)

class BlinkStreamBroker:
    """Starts one Blink live-view session per consumer."""

    def __init__(self, client: BlinkClient):
        self.client = client

    async def start_liveview(self, slug: str) -> LiveViewHandle:
        camera = self.client.camera_for_slug(slug)
        response = await self._request_liveview(camera)
        server = response.get("server", "")
        if not str(server).startswith("immis://"):
            raise RuntimeError(f"Unsupported liveview server URL: {server}")

        stream = TokenAwareBlinkLiveStream(
            camera,
            response,
            send_token=bool(self.client.config.get("send_liveview_token", False)),
        )
        await stream.start(host="127.0.0.1", port=None)
        feed_task = asyncio.create_task(stream.feed(), name=f"blink-feed-{slug}")
        LOGGER.info(
            "Started Blink liveview for %s: %s",
            slug,
            redact_liveview_response(response),
        )
        return LiveViewHandle(stream=stream, feed_task=feed_task, config=self.client.config)

    async def _request_liveview(self, camera: Any) -> dict[str, Any]:
        prefer_v6 = bool(self.client.config.get("prefer_v6_liveview", True))
        camera_type = camera.camera_type or ""
        product_type = getattr(camera, "product_type", "") or ""
        blink = self.client._require_blink()

        if camera_type == "mini" or product_type == "owl":
            url = (
                f"{blink.urls.base_url}/api/v2/accounts/{blink.account_id}"
                f"/networks/{camera.network_id}/owls/{camera.camera_id}/liveview"
            )
            response = await blink_api.http_post(
                blink, url=url, data=json.dumps({"intent": "liveview"})
            )
            if isinstance(response, dict) and response.get("server"):
                return response
            LOGGER.warning(
                "v2 owl liveview request failed for %s: %s",
                camera.name,
                redact_liveview_response(response)
                if isinstance(response, dict)
                else response,
            )

        if prefer_v6 and not camera_type:
            url = (
                f"{blink.urls.base_url}/api/v6/accounts/{blink.account_id}"
                f"/networks/{camera.network_id}/cameras/{camera.camera_id}/liveview"
            )
            response = await blink_api.http_post(
                blink, url=url, data=json.dumps({"intent": "liveview"})
            )
            if isinstance(response, dict) and response.get("server"):
                return response
            LOGGER.warning("v6 liveview request failed for %s; falling back", camera.name)

        return await blink_api.request_camera_liveview(
            camera.sync.blink,
            camera.sync.network_id,
            camera.camera_id,
            camera_type=camera_type,
        )
