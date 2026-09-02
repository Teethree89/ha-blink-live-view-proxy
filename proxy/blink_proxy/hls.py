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
        self.log_path = self.directory / "ffmpeg.log"
        self.liveview: LiveViewHandle | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.active_liveview_keys: set[str] = set()
        self.started_at = time.monotonic()
        self.last_touch = self.started_at

    def touch(self) -> None:
        self.last_touch = time.monotonic()

    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def register_liveview_key(self, key: str) -> None:
        """Expose this HLS live-view handle to browser-session scoped features."""
        if self.liveview is None:
            return
        self.manager.active_liveviews[key] = self.liveview
        self.active_liveview_keys.add(key)

    async def start(self) -> None:
        if self.directory.exists():
            shutil.rmtree(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)

        self.liveview = await self.manager.broker.start_liveview(self.slug)
        segment_pattern = self.directory / "segment_%05d.ts"

        # Blink's GOP is 4s, so copied segments are 4s. Re-encoding forces a
        # keyframe every second; it costs an encode per stream, hence opt-in.
        transcode = bool(self.manager.config.get("hls_transcode", False))
        if transcode:
            codec_args = [
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-g", "30",
                "-sc_threshold", "0",
                "-force_key_frames", "expr:gte(t,n_forced*1)",
                # Uncapped, libx264 hit 8 Mbit/s on foliage and phones stalled.
                "-maxrate", "2000k",
                "-bufsize", "2000k",
                # If the probe misses the frame rate ffmpeg guesses 90000 fps
                # and never finishes a segment.
                "-r", str(self.manager.config.get("hls_frame_rate", 24)),
                "-c:a", "copy",
            ]
        else:
            codec_args = ["-c", "copy"]
        # iOS wants about six seconds of playlist before it starts.
        list_size = "6" if transcode else "4"

        log_handle = open(self.log_path, "wb")
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.manager.config["ffmpeg"],
                "-hide_banner",
                "-loglevel",
                str(self.manager.config.get("ffmpeg_loglevel", "warning")),
                "-flags",
                "low_delay",
                # ffmpeg's default 5s probe put the first segment at the third
                # keyframe, and -fflags nobuffer would have discarded the first.
                "-probesize",
                str(self.manager.config.get("ffmpeg_probesize", 1_000_000)),
                "-analyzeduration",
                str(self.manager.config.get("ffmpeg_analyzeduration", 500_000)),
                "-i",
                self.liveview.tcp_url,
                *codec_args,
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_list_size",
                list_size,
                "-hls_flags",
                "delete_segments+omit_endlist+program_date_time",
                "-hls_segment_filename",
                str(segment_pattern),
                str(self.playlist),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=log_handle,
            )
        except Exception:
            if self.liveview is not None:
                await self.liveview.close()
            if self.directory.exists():
                shutil.rmtree(self.directory)
            raise
        finally:
            # ffmpeg holds its own duplicate of the descriptor, so the parent
            # copy is finished with either way.
            log_handle.close()
        LOGGER.info("Started ffmpeg HLS session for %s in %s", self.slug, self.directory)

    def _ffmpeg_error_tail(self, limit: int = 600) -> str:
        """Return the end of ffmpeg's stderr, or "" if there is nothing to show."""
        try:
            text = self.log_path.read_text(errors="replace").strip()
        except OSError:
            return ""
        return text[-limit:]

    async def wait_ready(self) -> None:
        timeout = float(self.manager.config.get("hls_start_timeout", 30))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # A client waiting here is active; keep the idle reaper off it.
            self.touch()
            if self.process and self.process.returncode is not None:
                tail = self._ffmpeg_error_tail()
                LOGGER.error(
                    "ffmpeg exited with %s for %s%s",
                    self.process.returncode,
                    self.slug,
                    f": {tail}" if tail else " (stderr was empty)",
                )
                raise RuntimeError(f"ffmpeg exited with {self.process.returncode}")
            if self.playlist.exists() and self.playlist.stat().st_size > 0:
                return
            await asyncio.sleep(0.2)
        tail = self._ffmpeg_error_tail()
        LOGGER.error(
            "HLS playlist never appeared for %s after %.0fs and ffmpeg is still "
            "running%s",
            self.slug,
            timeout,
            f". stderr tail: {tail}" if tail else " (stderr was empty)",
        )
        raise TimeoutError(f"HLS playlist not ready after {timeout:g}s")

    async def stop(self) -> None:
        for key in self.active_liveview_keys:
            if self.manager.active_liveviews.get(key) is self.liveview:
                self.manager.active_liveviews.pop(key, None)
        self.active_liveview_keys.clear()
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

    def __init__(
        self,
        broker: BlinkStreamBroker,
        config: dict[str, Any],
        base: Path,
        active_liveviews: dict[str, LiveViewHandle] | None = None,
    ):
        self.broker = broker
        self.config = config
        self.root_dir = resolve_path(config["hls_dir"], base)
        self.active_liveviews = active_liveviews if active_liveviews is not None else {}
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
