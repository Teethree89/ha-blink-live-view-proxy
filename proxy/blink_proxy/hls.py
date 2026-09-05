"""On-demand HLS session management."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .blink import BlinkStreamBroker, LiveViewHandle
from .config import resolve_path
from .constants import LOGGER_NAME
from .liveview_cache import last_liveview_metadata_from_path
from .util import liveview_filename

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
        # The same cached copy the MPEG-TS path keeps, written as a second
        # ffmpeg output. Without it saving a live view had nothing to finalize on
        # any client that plays HLS - which is every iPhone and iPad, since
        # they have no Media Source Extensions and never take the MPEG-TS
        # route. Worse than the error it showed: "Save MP4" would then hand
        # back whatever older session happened to be in the cache.
        self.cache_final: Path | None = None
        self.cache_tmp: Path | None = None
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

        cache_args: list[str] = []
        if bool(self.manager.config.get("save_liveview_cache", True)):
            cache_root = self.manager.liveview_cache_dir
            cache_root.mkdir(parents=True, exist_ok=True)
            self.cache_final = cache_root / liveview_filename(
                self.slug, datetime.datetime.now(datetime.timezone.utc)
            )
            self.cache_tmp = self.cache_final.with_suffix(".ts.part")
            # A second output on the same input, always a straight copy: the
            # cache should hold what Blink sent, not the re-encode low-latency
            # mode may be doing for the playlist.
            cache_args = [
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-c", "copy",
                "-f", "mpegts",
                str(self.cache_tmp),
            ]

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
                # Some cameras open with pictures seconds apart, so the analysis
                # can end without one. Left to auto-map, ffmpeg then drops video.
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
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
                *cache_args,
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
        self._finalize_cache()
        if self.directory.exists():
            shutil.rmtree(self.directory)
        LOGGER.info("Stopped HLS session for %s", self.slug)

    def _finalize_cache(self) -> None:
        """Publish the cached copy and make it the camera's last live view.

        Only on the way out, and only when ffmpeg actually wrote something: a
        half-named file would be picked up as the newest live view and handed
        to whoever asked to save one. The previous file goes and last_liveviews
        is repointed, the same two steps the MPEG-TS route takes, so Save gets
        this session rather than the first one ever recorded.
        """
        if self.cache_tmp is None or self.cache_final is None:
            return
        tmp, final = self.cache_tmp, self.cache_final
        self.cache_tmp = self.cache_final = None
        try:
            if tmp.exists() and tmp.stat().st_size > 0:
                previous = self.manager.last_liveviews.get(self.slug, {})
                previous_path = previous.get("path")
                if previous_path:
                    # A previous file that will not delete must not cost the new one.
                    with contextlib.suppress(OSError):
                        Path(previous_path).unlink()
                    with contextlib.suppress(OSError):
                        Path(previous_path).with_suffix(".mp4").unlink()
                os.replace(tmp, final)
                self.manager.last_liveviews[self.slug] = (
                    last_liveview_metadata_from_path(self.slug, final)
                )
                LOGGER.info("Cached the HLS live view for %s at %s", self.slug, final)
                return
        except OSError:
            LOGGER.warning("Could not finalize the cached live view for %s", self.slug)
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()

class HlsManager:
    """Keeps HLS sessions warm while HA is actively polling them."""

    def __init__(
        self,
        broker: BlinkStreamBroker,
        config: dict[str, Any],
        base: Path,
        active_liveviews: dict[str, LiveViewHandle] | None = None,
        last_liveviews: dict[str, dict[str, Any]] | None = None,
    ):
        self.broker = broker
        self.config = config
        # The same map the MPEG-TS path writes, so a saved HLS session becomes
        # the one find_last_liveview returns instead of an older file on disk.
        self.last_liveviews = last_liveviews if last_liveviews is not None else {}
        self.root_dir = resolve_path(config["hls_dir"], base)
        # Tolerate a config that does not name it: the tests build a manager
        # from a handful of keys, and a missing one must not be fatal here.
        cache_dir = config.get("liveview_cache_dir")
        self.liveview_cache_dir = (
            resolve_path(cache_dir, base) if cache_dir else self.root_dir.parent / "liveviews"
        )
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

    async def stop_session(self, slug: str) -> bool:
        """Stop one camera's session now. True if there was one to stop.

        The idle timeout is a backstop for a viewer that walked away, not the
        way a deliberate close should work. Waiting for it means the Blink
        camera keeps streaming for the whole timeout after nobody is watching
        - battery, on a battery camera - and it means the cached copy of the
        live view is not finalized until then either, so anything asking to
        save what was just watched finds nothing and gives up long before.
        """
        async with self.lock:
            session = self.sessions.pop(slug, None)
        if session is None:
            return False
        await session.stop()
        return True

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
