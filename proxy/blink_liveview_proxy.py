#!/usr/bin/env python3
"""Blink immis:// live-view proxy for Home Assistant.

The service logs in with BlinkPy, requests a Blink live-view session, bridges the
Walnut/IMMI MPEG-TS payload, and exposes it as either raw MPEG-TS or on-demand
HLS. Keep this on a trusted network; camera streams are sensitive.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import hashlib
import json
import logging
import os
import secrets
import shutil
import signal
import ssl
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import certifi
from aiohttp import ClientSession, TCPConnector, WSMsgType, web
from blinkpy import api as blink_api
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink
from blinkpy.livestream import BlinkLiveStream

LOGGER = logging.getLogger("blink_liveview_proxy")
APP_ROOT = Path(__file__).resolve().parent
IMMI_HEADER_BYTES = 9
MAX_IMMI_PAYLOAD_BYTES = 1024 * 1024
IMMI_DATA_FLAG_AUDIO = 0x05
IMMI_DATA_FLAG_AUDIO_CONFIG = 0x0C
IMMI_DATA_FLAG_SESSION_LV_CMD = 0x17
IMMI_AUDIO_CONFIG_SEQUENCE = 0xA0000001
LIVEVIEW_SESSION_COMMAND_START_AUDIO = 3
LIVEVIEW_SESSION_COMMAND_STOP_AUDIO = 4
AUDIO_CLOCK_RATE = 90_000
AAC_FRAME_SAMPLES = 1024
PTT_TARGET_SAMPLE_RATE = 16_000

DEFAULT_CONFIG: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8088,
    "auth_file": "secrets/blink-auth.json",
    "username_env": "BLINK_USERNAME",
    "password_env": "BLINK_PASSWORD",
    "twofa_env": "BLINK_2FA_CODE",
    "proxy_token_env": "BLINK_PROXY_TOKEN",
    "ffmpeg": "ffmpeg",
    "ffmpeg_loglevel": "warning",
    "hls_dir": ".runtime/blink-liveview-proxy",
    "hls_idle_timeout": 45,
    "hls_start_timeout": 30,
    "liveview_cache_dir": ".runtime/blink-liveview-proxy/liveviews",
    "mpegts_session_seconds": 60,
    "mpegts_cooldown_seconds": 30,
    "save_liveview_cache": True,
    "ptt_aac_bitrate": "40k",
    "ptt_strip_adts": False,
    "ptt_send_audio_config": False,
    "ptt_disabled_camera_types": ["mini"],
    "ptt_disabled_product_types": ["owl"],
    "prefer_v6_liveview": True,
    "send_liveview_token": False,
    "cameras": {},
}


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge dictionaries without mutating inputs."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


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


def load_config(path: Path | None) -> tuple[dict[str, Any], Path]:
    """Load JSON config and return the config plus relative path base."""
    if path is None:
        env_path = os.getenv("BLINK_PROXY_CONFIG", "")
        path = Path(env_path) if env_path else APP_ROOT / "config.json"
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            return deep_merge(DEFAULT_CONFIG, json.load(handle)), path.parent
    return dict(DEFAULT_CONFIG), APP_ROOT


def resolve_path(value: str | os.PathLike[str], base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def create_client_session() -> ClientSession:
    """Create an aiohttp session with certifi roots for macOS Python builds."""
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    return ClientSession(connector=TCPConnector(ssl=ssl_context))


def normalize_slug(value: str) -> str:
    lowered = value.strip().lower()
    slug = []
    previous_sep = False
    for char in lowered:
        if char.isalnum():
            slug.append(char)
            previous_sep = False
        elif not previous_sep:
            slug.append("_")
            previous_sep = True
    return "".join(slug).strip("_") or "camera"


def filename_safe(value: str) -> str:
    return normalize_slug(value).replace("_", "-")


def redact_liveview_response(response: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(response)
    if "server" in redacted:
        parsed = urllib.parse.urlparse(str(redacted["server"]))
        redacted["server"] = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}/..."
    if "liveview_token" in redacted:
        redacted["liveview_token"] = "<redacted>"
    return redacted


def parse_blink_time(value: str | datetime.datetime) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        timestamp = value
    else:
        timestamp = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
    return timestamp.astimezone(datetime.timezone.utc)


def clip_filename(camera_name: str, created_at: datetime.datetime, source: str) -> str:
    local = created_at.astimezone()
    stamp = local.strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{filename_safe(camera_name)}_{source}.mp4"


def liveview_filename(slug: str, started_at: datetime.datetime) -> str:
    local = started_at.astimezone()
    stamp = local.strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{filename_safe(slug)}_liveview.ts"


def liveview_glob(slug: str) -> str:
    return f"*_{filename_safe(slug)}_liveview.ts"


def last_liveview_metadata_from_path(slug: str, path: Path) -> dict[str, Any]:
    """Return cached live-view metadata for a file on disk."""
    stat = path.stat()
    ended_at = datetime.datetime.fromtimestamp(stat.st_mtime, datetime.timezone.utc)
    return {
        "slug": slug,
        "path": str(path),
        "filename": path.name,
        "started_at": None,
        "ended_at": ended_at.isoformat(),
        "duration": None,
        "bytes": stat.st_size,
    }


def find_last_liveview(app: web.Application, slug: str) -> dict[str, Any] | None:
    """Return the latest cached live view for a slug, if one exists."""
    last = app["last_liveviews"].get(slug)
    if last and Path(last["path"]).exists():
        return last

    cache_root: Path = app["liveview_cache_dir"]
    candidates = sorted(
        cache_root.glob(liveview_glob(slug)),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        if last:
            app["last_liveviews"].pop(slug, None)
        return None

    last = last_liveview_metadata_from_path(slug, candidates[0])
    app["last_liveviews"][slug] = last
    return last


async def ensure_last_liveview_mp4(
    app: web.Application, slug: str, ts_path: Path
) -> Path:
    """Remux a cached MPEG-TS live view to MP4 and return the MP4 path."""
    mp4_path = ts_path.with_suffix(".mp4")
    if (
        mp4_path.exists()
        and mp4_path.stat().st_size > 0
        and mp4_path.stat().st_mtime >= ts_path.stat().st_mtime
    ):
        return mp4_path

    locks: dict[str, asyncio.Lock] = app["mp4_locks"]
    lock = locks.setdefault(str(ts_path), asyncio.Lock())
    async with lock:
        if (
            mp4_path.exists()
            and mp4_path.stat().st_size > 0
            and mp4_path.stat().st_mtime >= ts_path.stat().st_mtime
        ):
            return mp4_path

        tmp_path = mp4_path.with_suffix(".mp4.part")
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()

        process = await asyncio.create_subprocess_exec(
            str(app["config"]["ffmpeg"]),
            "-hide_banner",
            "-y",
            "-loglevel",
            str(app["config"].get("ffmpeg_loglevel", "warning")),
            "-i",
            str(ts_path),
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-dn",
            "-sn",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            "-avoid_negative_ts",
            "make_zero",
            "-f",
            "mp4",
            str(tmp_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode != 0:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            stderr_text = (stderr or b"").decode("utf-8", "replace").strip()
            LOGGER.warning(
                "ffmpeg failed to remux last live view for %s: %s",
                slug,
                stderr_text[-2000:],
            )
            raise web.HTTPBadGateway(
                text=f"Could not convert cached live view to MP4 for {slug}\n"
            )

        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            raise web.HTTPBadGateway(
                text=f"MP4 conversion produced no output for {slug}\n"
            )

        os.replace(tmp_path, mp4_path)
        LOGGER.info("Remuxed last live view for %s to %s", slug, mp4_path)
        return mp4_path


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


class ClipManager:
    """Lists and downloads cloud and Sync Module local-storage clips."""

    def __init__(self, client: BlinkClient):
        self.client = client

    def _slug_for_name(self, camera_name: str) -> str:
        blink = self.client._require_blink()
        for name, camera in blink.cameras.items():
            if str(name).casefold() == str(camera_name).casefold():
                return self.client.camera_slug(camera, name)
            if str(camera.name).casefold() == str(camera_name).casefold():
                return self.client.camera_slug(camera, name)
        return normalize_slug(camera_name)

    async def list_clips(
        self,
        source: str = "both",
        camera_slug: str | None = None,
        hours: float = 24,
        pages: int = 3,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            hours=hours
        )
        clips: list[dict[str, Any]] = []

        if source in ("both", "cloud"):
            clips.extend(await self._cloud_clips(since, camera_slug, pages))
        if source in ("both", "local"):
            clips.extend(await self._local_storage_clips(since, camera_slug))

        clips.sort(key=lambda clip: clip["created_at"], reverse=True)
        if limit is not None:
            clips = clips[:limit]
        return clips

    async def _cloud_clips(
        self,
        since: datetime.datetime,
        camera_slug: str | None,
        pages: int,
    ) -> list[dict[str, Any]]:
        blink = self.client._require_blink()
        clips: list[dict[str, Any]] = []

        for page in range(1, pages + 1):
            response = await blink_api.request_videos(
                blink, time=since.timestamp(), page=page
            )
            media = response.get("media", []) if isinstance(response, dict) else []
            if not media:
                break

            for item in media:
                if item.get("deleted"):
                    continue
                camera_name = item.get("device_name") or item.get("camera_name")
                media_path = item.get("media")
                created_raw = item.get("created_at")
                if not camera_name or not media_path or not created_raw:
                    continue

                slug = self._slug_for_name(camera_name)
                if camera_slug and slug != camera_slug:
                    continue

                created_at = parse_blink_time(created_raw)
                if created_at < since:
                    continue

                clips.append(
                    {
                        "source": "cloud",
                        "slug": slug,
                        "camera_name": camera_name,
                        "created_at": created_at,
                        "size": item.get("size"),
                        "url": f"{blink.urls.base_url}{media_path}",
                    }
                )
        return clips

    async def _local_storage_clips(
        self, since: datetime.datetime, camera_slug: str | None
    ) -> list[dict[str, Any]]:
        blink = self.client._require_blink()
        clips: list[dict[str, Any]] = []

        for sync_name, sync_module in blink.sync.items():
            if not getattr(sync_module, "local_storage", False):
                continue

            LOGGER.info("Refreshing local storage manifest for %s", sync_name)
            await sync_module.update_local_storage_manifest()
            storage = getattr(sync_module, "_local_storage", {})
            manifest_id = storage.get("last_manifest_id")
            manifest = list(storage.get("manifest", []))

            for item in manifest:
                camera_name = item.name
                slug = self._slug_for_name(camera_name)
                if camera_slug and slug != camera_slug:
                    continue

                created_at = parse_blink_time(item.created_at)
                if created_at < since:
                    continue

                clips.append(
                    {
                        "source": "local",
                        "slug": slug,
                        "camera_name": camera_name,
                        "created_at": created_at,
                        "size": item.size,
                        "url": f"{blink.urls.base_url}{item.url(manifest_id)}",
                        "item": item,
                    }
                )
        return clips

    async def save_clip(self, clip: dict[str, Any], output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        response = await self.open_clip_response(clip)

        created_at = clip["created_at"]
        path = output_dir / clip_filename(
            clip["camera_name"], created_at, clip["source"]
        )
        try:
            path.write_bytes(await response.read())
        finally:
            response.close()
        return path

    async def open_clip_response(self, clip: dict[str, Any]):
        """Return an open HTTP response for the requested Blink clip."""
        blink = self.client._require_blink()

        item = clip.get("item")
        if item is not None:
            LOGGER.info("Preparing local storage clip %s", item.id)
            await item.prepare_download(blink)

        response = await blink_api.http_get(
            blink,
            url=clip["url"],
            stream=True,
            json=False,
            timeout=90,
        )
        if response is None:
            raise RuntimeError(f"Failed to download {clip['url']}")
        if getattr(response, "status", None) != 200:
            try:
                body = await response.text()
            except Exception:  # noqa: BLE001
                body = ""
            response.close()
            message = f"Download returned HTTP {response.status}: {clip['url']}"
            if body:
                message = f"{message}\n{body}"
            raise RuntimeError(message)
        return response


class HlsSession:
    """Owns the Blink live-view and ffmpeg process for one HLS camera session."""

    def __init__(self, slug: str, manager: "HlsManager"):
        self.slug = slug
        self.manager = manager
        self.directory = manager.root_dir / slug
        self.playlist = self.directory / "index.m3u8"
        self.liveview: LiveViewHandle | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.started_at = time.monotonic()
        self.last_touch = self.started_at

    def touch(self) -> None:
        self.last_touch = time.monotonic()

    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        if self.directory.exists():
            shutil.rmtree(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)

        self.liveview = await self.manager.broker.start_liveview(self.slug)
        segment_pattern = self.directory / "segment_%05d.ts"
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.manager.config["ffmpeg"],
                "-hide_banner",
                "-loglevel",
                str(self.manager.config.get("ffmpeg_loglevel", "warning")),
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-i",
                self.liveview.tcp_url,
                "-c",
                "copy",
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_list_size",
                "4",
                "-hls_flags",
                "delete_segments+omit_endlist+program_date_time",
                "-hls_segment_filename",
                str(segment_pattern),
                str(self.playlist),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception:
            if self.liveview is not None:
                await self.liveview.close()
            if self.directory.exists():
                shutil.rmtree(self.directory)
            raise
        LOGGER.info("Started ffmpeg HLS session for %s in %s", self.slug, self.directory)

    async def wait_ready(self) -> None:
        timeout = float(self.manager.config.get("hls_start_timeout", 30))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process and self.process.returncode is not None:
                raise RuntimeError(f"ffmpeg exited with {self.process.returncode}")
            if self.playlist.exists() and self.playlist.stat().st_size > 0:
                return
            await asyncio.sleep(0.2)
        raise TimeoutError(f"HLS playlist not ready after {timeout:g}s")

    async def stop(self) -> None:
        if self.process:
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            else:
                await self.process.wait()
        if self.liveview is not None:
            await self.liveview.close()
        if self.directory.exists():
            shutil.rmtree(self.directory)
        LOGGER.info("Stopped HLS session for %s", self.slug)


class HlsManager:
    """Keeps HLS sessions warm while HA is actively polling them."""

    def __init__(self, broker: BlinkStreamBroker, config: dict[str, Any], base: Path):
        self.broker = broker
        self.config = config
        self.root_dir = resolve_path(config["hls_dir"], base)
        self.sessions: dict[str, HlsSession] = {}
        self.lock = asyncio.Lock()

    async def get_or_start(self, slug: str) -> HlsSession:
        async with self.lock:
            session = self.sessions.get(slug)
            if session and not session.is_running():
                await session.stop()
                session = None
            if session is None:
                session = HlsSession(slug, self)
                await session.start()
                self.sessions[slug] = session
            session.touch()
            return session

    async def get_existing(self, slug: str) -> HlsSession | None:
        async with self.lock:
            session = self.sessions.get(slug)
            if session:
                session.touch()
            return session

    async def cleanup_loop(self) -> None:
        idle_timeout = float(self.config.get("hls_idle_timeout", 45))
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            stale: list[tuple[str, HlsSession]] = []
            async with self.lock:
                for slug, session in list(self.sessions.items()):
                    if now - session.last_touch > idle_timeout or not session.is_running():
                        stale.append((slug, session))
                        self.sessions.pop(slug, None)
            for _slug, session in stale:
                await session.stop()

    async def stop_all(self) -> None:
        async with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            await session.stop()


def check_authorized(request: web.Request) -> None:
    token = request.app.get("proxy_token")
    if not token:
        return

    provided = request.query.get("token", "")
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        provided = auth_header.split(" ", 1)[1]
    if not secrets.compare_digest(str(token), str(provided)):
        raise web.HTTPUnauthorized(text="Missing or invalid proxy token\n")


def rewrite_playlist_for_token(text: str, token: str | None) -> str:
    if not token:
        return text
    quoted = urllib.parse.quote(token, safe="")
    lines = []
    for line in text.splitlines():
        if line and not line.startswith("#") and "?" not in line:
            lines.append(f"{line}?token={quoted}")
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


async def index_handler(request: web.Request) -> web.Response:
    check_authorized(request)
    rows = request.app["client"].list_cameras()
    return web.json_response(
        {
            "service": "blink-liveview-proxy",
            "cameras": rows,
            "notes": [
                "HLS: /cameras/{slug}/hls/index.m3u8",
                "MPEG-TS: /cameras/{slug}/mpegts",
                "Last live view info: /cameras/{slug}/last-liveview",
                "Last live view download: /cameras/{slug}/last-liveview.ts",
                "Last live view MP4 download: /cameras/{slug}/last-liveview.mp4",
                "Recent clips: /clips?source=local&hours=24&limit=20",
            ],
        }
    )


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def cameras_handler(request: web.Request) -> web.Response:
    check_authorized(request)
    return web.json_response({"cameras": request.app["client"].list_cameras()})


def _clamped_float(value: str | None, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _clamped_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


async def clips_handler(request: web.Request) -> web.Response:
    check_authorized(request)
    source = request.query.get("source", "local")
    if source not in {"both", "cloud", "local"}:
        raise web.HTTPBadRequest(text="source must be one of both, cloud, or local\n")

    camera_slug = request.query.get("camera") or None
    hours = _clamped_float(request.query.get("hours"), 24, 0.1, 24 * 30)
    pages = _clamped_int(request.query.get("pages"), 3, 1, 10)
    limit = _clamped_int(request.query.get("limit"), 20, 1, 100)

    manager = ClipManager(request.app["client"])
    clips = await manager.list_clips(
        source=source,
        camera_slug=camera_slug,
        hours=hours,
        pages=pages,
        limit=limit,
    )
    return web.json_response(
        {
            "count": len(clips),
            "source": source,
            "camera": camera_slug,
            "hours": hours,
            "clips": [
                printable_clip(
                    clip,
                    download_url=clip_download_url(
                        clip,
                        hours=hours,
                        pages=pages,
                        limit=max(limit, 100),
                    ),
                )
                for clip in clips
            ],
        }
    )


async def clip_download_handler(request: web.Request) -> web.Response:
    check_authorized(request)
    clip_key = request.match_info["clip_id"]
    source = request.query.get("source", "local")
    if source not in {"cloud", "local"}:
        raise web.HTTPBadRequest(text="source must be cloud or local\n")

    camera_slug = request.query.get("camera") or None
    hours = _clamped_float(request.query.get("hours"), 24, 0.1, 24 * 30)
    pages = _clamped_int(request.query.get("pages"), 3, 1, 10)
    limit = _clamped_int(request.query.get("limit"), 200, 1, 500)

    manager = ClipManager(request.app["client"])
    clips = await manager.list_clips(
        source=source,
        camera_slug=camera_slug,
        hours=hours,
        pages=pages,
        limit=limit,
    )
    clip = next((item for item in clips if clip_id(item) == clip_key), None)
    if clip is None:
        raise web.HTTPNotFound(text="Clip was not found in the current manifest\n")

    try:
        upstream = await manager.open_clip_response(clip)
    except RuntimeError as err:
        raise web.HTTPBadGateway(text=f"{err}\n") from err

    filename = clip_filename(clip["camera_name"], clip["created_at"], clip["source"])
    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Accel-Buffering": "no",
    }
    content_length = upstream.headers.get("Content-Length")
    if content_length:
        headers["Content-Length"] = content_length

    response = web.StreamResponse(status=200, headers=headers)
    response.content_type = upstream.headers.get("Content-Type", "video/mp4")
    try:
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(102400):
            await response.write(chunk)
    except (ConnectionResetError, TimeoutError):
        LOGGER.debug("Browser clip download closed for %s", clip_key)
    finally:
        upstream.close()
    return response


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


async def mpegts_handler(request: web.Request) -> web.StreamResponse:
    check_authorized(request)
    slug = request.match_info["slug"]
    broker: BlinkStreamBroker = request.app["broker"]
    session = request.query.get("session") or secrets.token_urlsafe(16)
    active_key = liveview_session_key(slug, session)
    force = request.query.get("force", "").lower() in ("1", "true", "yes")
    max_seconds = float(
        request.query.get(
            "seconds",
            request.app["config"].get("mpegts_session_seconds", 60),
        )
        or 0
    )
    cooldowns: dict[str, float] = request.app["mpegts_cooldowns"]
    now = time.monotonic()
    cooldown_until = cooldowns.get(slug, 0)
    if not force and cooldown_until > now:
        retry_after = max(1, int(cooldown_until - now))
        raise web.HTTPTooManyRequests(
            text=f"Live view cooldown active for {slug}; retry in {retry_after}s\n",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        liveview = await broker.start_liveview(slug)
    except KeyError as exc:
        raise web.HTTPNotFound(text=f"Unknown camera slug: {slug}\n") from exc
    request.app["active_liveviews"][active_key] = liveview
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    cache_handle: Any | None = None
    cache_tmp: Path | None = None
    cache_final: Path | None = None
    started_at = datetime.datetime.now(datetime.timezone.utc)
    bytes_written = 0

    if bool(request.app["config"].get("save_liveview_cache", True)):
        cache_root: Path = request.app["liveview_cache_dir"]
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_final = cache_root / liveview_filename(slug, started_at)
        cache_tmp = cache_final.with_suffix(".ts.part")
        cache_handle = cache_tmp.open("wb")

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "video/mp2t",
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    try:
        reader, writer = await asyncio.open_connection(liveview.host, liveview.port)
        deadline = time.monotonic() + max_seconds if max_seconds > 0 else None
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    LOGGER.info(
                        "MPEG-TS session reached %.0fs limit for %s",
                        max_seconds,
                        slug,
                    )
                    break
                chunk = await asyncio.wait_for(
                    reader.read(188 * 64),
                    timeout=remaining,
                )
            else:
                chunk = await reader.read(188 * 64)
            if not chunk:
                break
            if cache_handle is not None:
                cache_handle.write(chunk)
                bytes_written += len(chunk)
            await response.write(chunk)
    except asyncio.TimeoutError:
        LOGGER.info("MPEG-TS read timeout at session limit for %s", slug)
    except (ConnectionResetError, asyncio.CancelledError, BrokenPipeError):
        LOGGER.info("MPEG-TS client disconnected for %s", slug)
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if cache_handle is not None:
            cache_handle.close()
        if cache_tmp is not None and cache_final is not None:
            if bytes_written > 0:
                previous = request.app["last_liveviews"].get(slug, {})
                previous_path = previous.get("path")
                if previous_path:
                    with contextlib.suppress(FileNotFoundError):
                        Path(previous_path).unlink()
                    with contextlib.suppress(FileNotFoundError):
                        Path(previous_path).with_suffix(".mp4").unlink()
                os.replace(cache_tmp, cache_final)
                ended_at = datetime.datetime.now(datetime.timezone.utc)
                request.app["last_liveviews"][slug] = {
                    "slug": slug,
                    "path": str(cache_final),
                    "filename": cache_final.name,
                    "started_at": started_at.isoformat(),
                    "ended_at": ended_at.isoformat(),
                    "duration": (ended_at - started_at).total_seconds(),
                    "bytes": bytes_written,
                }
                LOGGER.info(
                    "Saved last live-view cache for %s to %s (%d bytes)",
                    slug,
                    cache_final,
                    bytes_written,
                )
            else:
                with contextlib.suppress(FileNotFoundError):
                    cache_tmp.unlink()
        cooldown_seconds = float(
            request.app["config"].get("mpegts_cooldown_seconds", 30) or 0
        )
        if cooldown_seconds > 0:
            cooldowns[slug] = time.monotonic() + cooldown_seconds
        if request.app["active_liveviews"].get(active_key) is liveview:
            request.app["active_liveviews"].pop(active_key, None)
        await liveview.close()
        with contextlib.suppress(Exception):
            await response.write_eof()
    return response


async def last_liveview_info_handler(request: web.Request) -> web.Response:
    check_authorized(request)
    slug = request.match_info["slug"]
    last = find_last_liveview(request.app, slug)
    cooldown_until = request.app["mpegts_cooldowns"].get(slug, 0)
    cooldown_remaining = max(0, int(cooldown_until - time.monotonic()))
    if not last:
        return web.json_response(
            {
                "available": False,
                "slug": slug,
                "cooldown_remaining": cooldown_remaining,
            }
        )

    payload = {
        key: value for key, value in last.items() if key != "path"
    }
    payload.update(
        {
            "available": Path(last["path"]).exists(),
            "download_url": f"/cameras/{slug}/last-liveview.ts",
            "mp4_download_url": f"/cameras/{slug}/last-liveview.mp4",
            "cooldown_remaining": cooldown_remaining,
        }
    )
    return web.json_response(payload)


async def last_liveview_download_handler(request: web.Request) -> web.FileResponse:
    check_authorized(request)
    slug = request.match_info["slug"]
    last = find_last_liveview(request.app, slug)
    if not last:
        raise web.HTTPNotFound(text=f"No cached live view for {slug}\n")
    path = Path(last["path"])
    return web.FileResponse(
        path,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{path.name}"',
        },
    )


async def last_liveview_mp4_download_handler(request: web.Request) -> web.FileResponse:
    check_authorized(request)
    slug = request.match_info["slug"]
    last = find_last_liveview(request.app, slug)
    if not last:
        raise web.HTTPNotFound(text=f"No cached live view for {slug}\n")
    ts_path = Path(last["path"])
    mp4_path = await ensure_last_liveview_mp4(request.app, slug, ts_path)
    return web.FileResponse(
        mp4_path,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{mp4_path.name}"',
        },
    )


async def hls_playlist_handler(request: web.Request) -> web.Response:
    check_authorized(request)
    slug = request.match_info["slug"]
    manager: HlsManager = request.app["hls_manager"]
    try:
        session = await manager.get_or_start(slug)
    except KeyError as exc:
        raise web.HTTPNotFound(text=f"Unknown camera slug: {slug}\n") from exc
    await session.wait_ready()

    text = session.playlist.read_text(encoding="utf-8")
    token = request.query.get("token") if request.app.get("proxy_token") else None
    text = rewrite_playlist_for_token(text, token)
    return web.Response(
        text=text,
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


async def hls_segment_handler(request: web.Request) -> web.FileResponse:
    check_authorized(request)
    slug = request.match_info["slug"]
    filename = request.match_info["filename"]
    if "/" in filename or not filename.endswith(".ts"):
        raise web.HTTPNotFound()

    manager: HlsManager = request.app["hls_manager"]
    session = await manager.get_existing(slug)
    if session is None:
        raise web.HTTPNotFound(text="No active HLS session\n")

    path = session.directory / filename
    if not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def make_app(
    config: dict[str, Any], config_base: Path, pin: str | None
) -> web.Application:
    client = BlinkClient(config, config_base, pin)
    await client.start()

    broker = BlinkStreamBroker(client)
    hls_manager = HlsManager(broker, config, config_base)
    proxy_token = os.getenv(config["proxy_token_env"], config.get("proxy_token", ""))

    app = web.Application()
    app["client"] = client
    app["broker"] = broker
    app["hls_manager"] = hls_manager
    app["proxy_token"] = proxy_token
    app["config"] = config
    app["liveview_cache_dir"] = resolve_path(config["liveview_cache_dir"], config_base)
    app["last_liveviews"] = {}
    app["mpegts_cooldowns"] = {}
    app["active_liveviews"] = {}
    app["mp4_locks"] = {}

    app.router.add_get("/", index_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/cameras", cameras_handler)
    app.router.add_get("/clips", clips_handler)
    app.router.add_get("/clips/{clip_id}.mp4", clip_download_handler)
    app.router.add_get("/cameras/{slug}/mpegts", mpegts_handler)
    app.router.add_get("/cameras/{slug}/ptt", ptt_handler)
    app.router.add_get("/cameras/{slug}/last-liveview", last_liveview_info_handler)
    app.router.add_get(
        "/cameras/{slug}/last-liveview.ts", last_liveview_download_handler
    )
    app.router.add_get(
        "/cameras/{slug}/last-liveview.mp4", last_liveview_mp4_download_handler
    )
    app.router.add_get("/cameras/{slug}/hls/index.m3u8", hls_playlist_handler)
    app.router.add_get("/cameras/{slug}/hls/{filename}", hls_segment_handler)

    async def cleanup_context(_app: web.Application):
        cleanup_task = asyncio.create_task(hls_manager.cleanup_loop())
        try:
            yield
        finally:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
            await hls_manager.stop_all()
            await client.close()

    app.cleanup_ctx.append(cleanup_context)
    return app


async def command_list(args: argparse.Namespace) -> int:
    config, base = load_config(args.config)
    client = BlinkClient(config, base, args.pin)
    try:
        await client.start()
        print(json.dumps({"cameras": client.list_cameras()}, indent=2, sort_keys=True))
    finally:
        await client.close()
    return 0


def clip_id(clip: dict[str, Any]) -> str:
    """Return a stable, opaque ID for a Blink clip listing entry."""
    item = clip.get("item")
    item_id = str(getattr(item, "id", "") or "")
    created_at = clip["created_at"]
    if isinstance(created_at, datetime.datetime):
        created_at = created_at.isoformat()
    identity = "|".join(
        [
            str(clip.get("source", "")),
            str(clip.get("slug", "")),
            str(clip.get("camera_name", "")),
            str(created_at),
            str(clip.get("size", "")),
            item_id,
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def clip_download_url(
    clip: dict[str, Any],
    *,
    hours: float,
    pages: int,
    limit: int,
) -> str:
    """Return a proxy-relative download URL for a listed clip."""
    query = urllib.parse.urlencode(
        {
            "source": clip["source"],
            "camera": clip["slug"],
            "hours": str(hours),
            "pages": str(pages),
            "limit": str(limit),
        }
    )
    return f"/clips/{clip_id(clip)}.mp4?{query}"


def printable_clip(
    clip: dict[str, Any], download_url: str | None = None
) -> dict[str, Any]:
    row = {
        "id": clip_id(clip),
        "source": clip["source"],
        "slug": clip["slug"],
        "camera_name": clip["camera_name"],
        "created_at": clip["created_at"].isoformat(),
        "size": clip.get("size"),
    }
    if download_url:
        row["download_url"] = download_url
    return row


async def command_clips(args: argparse.Namespace) -> int:
    config, base = load_config(args.config)
    client = BlinkClient(config, base, args.pin)
    try:
        await client.start()
        manager = ClipManager(client)
        clips = await manager.list_clips(
            source=args.source,
            camera_slug=args.camera,
            hours=args.hours,
            pages=args.pages,
            limit=args.limit,
        )
        print(
            json.dumps(
                {"count": len(clips), "clips": [printable_clip(clip) for clip in clips]},
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        await client.close()
    return 0


async def command_save_clips(args: argparse.Namespace) -> int:
    config, base = load_config(args.config)
    client = BlinkClient(config, base, args.pin)
    saved: list[str] = []
    try:
        await client.start()
        manager = ClipManager(client)
        clips = await manager.list_clips(
            source=args.source,
            camera_slug=args.camera,
            hours=args.hours,
            pages=args.pages,
            limit=args.limit,
        )
        if not clips:
            print(json.dumps({"saved": [], "message": "No matching clips found"}))
            return 0

        output_dir = resolve_path(args.output_dir, base)
        for clip in clips:
            path = await manager.save_clip(clip, output_dir)
            saved.append(str(path))
            LOGGER.info("Saved %s clip to %s", clip["source"], path)

        print(json.dumps({"saved": saved}, indent=2, sort_keys=True))
    finally:
        await client.close()
    return 0


async def command_probe(args: argparse.Namespace) -> int:
    config, base = load_config(args.config)
    if args.liveview_token == "on":
        config["send_liveview_token"] = True
    elif args.liveview_token == "off":
        config["send_liveview_token"] = False
    client = BlinkClient(config, base, args.pin)
    bytes_seen = 0
    packets_seen = 0
    read_timeouts = 0
    liveview: LiveViewHandle | None = None
    writer: asyncio.StreamWriter | None = None

    try:
        await client.start()
        broker = BlinkStreamBroker(client)
        liveview = await broker.start_liveview(args.slug)
        reader, writer = await asyncio.open_connection(liveview.host, liveview.port)
        deadline = time.monotonic() + args.seconds
        output = open(args.output, "wb") if args.output else None
        try:
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                timeout = min(args.read_timeout, remaining)
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(188 * 64), timeout=timeout
                    )
                except TimeoutError:
                    read_timeouts += 1
                    LOGGER.debug(
                        "Still waiting for MPEG-TS bytes from %s (%d timeout%s)",
                        args.slug,
                        read_timeouts,
                        "" if read_timeouts == 1 else "s",
                    )
                    continue
                if not chunk:
                    break
                bytes_seen += len(chunk)
                packets_seen += sum(
                    1 for offset in range(0, len(chunk), 188) if chunk[offset] == 0x47
                )
                if output:
                    output.write(chunk)
        finally:
            if output:
                output.close()
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if liveview is not None:
            await liveview.close()
        await client.close()

    print(
        json.dumps(
            {
                "slug": args.slug,
                "seconds": args.seconds,
                "bytes": bytes_seen,
                "mpegts_sync_packets": packets_seen,
                "read_timeouts": read_timeouts,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


async def command_serve(args: argparse.Namespace) -> int:
    config, base = load_config(args.config)
    if args.host:
        config["host"] = args.host
    if args.port:
        config["port"] = args.port

    app = await make_app(config, base, args.pin)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config["host"], int(config["port"]))
    await site.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    LOGGER.info("Serving on http://%s:%s", config["host"], config["port"])
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to JSON config; defaults to BLINK_PROXY_CONFIG or config.json "
            "next to this script"
        ),
    )
    parser.add_argument("--pin", help="Blink 2FA code for first login")
    parser.add_argument("--log-level", default="INFO")

    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the HTTP proxy")
    serve.add_argument("--host", help="Override listen host")
    serve.add_argument("--port", type=int, help="Override listen port")
    serve.set_defaults(func=command_serve)

    list_cmd = subparsers.add_parser("list", help="List discovered Blink cameras")
    list_cmd.set_defaults(func=command_list)

    clips = subparsers.add_parser("clips", help="List recent cloud/local clips")
    clips.add_argument("--camera", help="Optional camera slug, for example driveway")
    clips.add_argument(
        "--source",
        choices=("both", "cloud", "local"),
        default="both",
        help="Clip source to inspect",
    )
    clips.add_argument("--hours", type=float, default=24)
    clips.add_argument("--pages", type=int, default=3, help="Cloud clip pages to scan")
    clips.add_argument("--limit", type=int, default=20)
    clips.set_defaults(func=command_clips)

    save_clips = subparsers.add_parser("save-clips", help="Save recent clips")
    save_clips.add_argument(
        "--camera", help="Optional camera slug, for example driveway"
    )
    save_clips.add_argument(
        "--source",
        choices=("both", "cloud", "local"),
        default="both",
        help="Clip source to inspect",
    )
    save_clips.add_argument("--hours", type=float, default=24)
    save_clips.add_argument(
        "--pages", type=int, default=3, help="Cloud clip pages to scan"
    )
    save_clips.add_argument("--limit", type=int, default=1)
    save_clips.add_argument("--output-dir", default="clips")
    save_clips.set_defaults(func=command_save_clips)

    probe = subparsers.add_parser("probe", help="Read live MPEG-TS bytes briefly")
    probe.add_argument("slug", help="Camera slug from config or list output")
    probe.add_argument("--seconds", type=float, default=10)
    probe.add_argument("--read-timeout", type=float, default=5)
    probe.add_argument("--output", help="Optional .ts capture path for ffprobe testing")
    probe.add_argument(
        "--liveview-token",
        choices=("config", "on", "off"),
        default="config",
        help="Override whether the IMMI auth header includes liveview_token",
    )
    probe.set_defaults(func=command_probe)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
