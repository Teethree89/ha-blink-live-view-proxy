"""On-demand HLS session management."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from .blink import BlinkStreamBroker, LiveViewHandle
from .config import resolve_path
from .constants import LOGGER_NAME

LOGGER = logging.getLogger(LOGGER_NAME)

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
