"""An on-disk cache of local clips, and the first-frame thumbnails cut from it.

Why a cache at all: a Sync Module clip is not a file the proxy can read part
of. blinkpy's prepare_download() asks the module to upload the whole clip to
Blink's cloud and polls until it has, then the bytes come back over HTTPS -
several seconds and a round of Blink API calls per clip, every time. The viewer
paid that on every Preview and again on every Download, and a thumbnail per
row would have paid it for everything on screen at once. So each clip is
fetched once, kept, and its thumbnail is cut from the copy on disk.

Serving from disk also means byte ranges work. Streaming straight from Blink
never supported them, and Safari will not play an MP4 from a server that does
not - so on an iPhone, Preview used to do nothing at all.

Bounded: past clip_cache_max_mb the oldest files go first. A thumbnail is a
few kilobytes beside the clip it came from, so what the cap really limits is
how many clips stay instantly replayable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from .clips import ClipManager, clip_id, printable_clip
from .constants import LOGGER_NAME

LOGGER = logging.getLogger(LOGGER_NAME)

THUMBNAIL_WIDTH = 480
CACHE_SUFFIXES = (".mp4", ".jpg", ".json")


def thumbnail_args(ffmpeg: str, loglevel: str, source: Path, target: Path) -> list[str]:
    """The ffmpeg invocation that cuts one frame; separate so a test can read it."""
    return [
        str(ffmpeg),
        "-hide_banner",
        "-y",
        "-loglevel",
        str(loglevel),
        "-i",
        str(source),
        # The first frame, not a "representative" one: the row should show what
        # the clip opens on, which is what tripped the motion event.
        "-frames:v",
        "1",
        # -2 keeps the height even, which the encoder needs.
        "-vf",
        f"scale={THUMBNAIL_WIDTH}:-2",
        # Named explicitly because the target is written under a .part name
        # first, and ffmpeg would otherwise guess the codec from that suffix.
        "-c:v",
        "mjpeg",
        "-pix_fmt",
        "yuvj420p",
        "-q:v",
        "4",
        "-f",
        "image2",
        "-update",
        "1",
        str(target),
    ]


def prune_directory(
    root: Path, max_bytes: int, keep: set[Path] | None = None
) -> list[Path]:
    """Delete the oldest files until the directory fits, and return what went.

    Oldest by modification time. A download and a thumbnail both set it, and
    serving a cached clip touches it, so what gets evicted is what nobody has
    asked for in the longest - not what happened to be recorded first.
    """
    if not root.exists():
        return []
    files = [
        path
        for path in root.iterdir()
        if path.is_file() and path.suffix in CACHE_SUFFIXES + (".part",)
    ]
    total = sum(path.stat().st_size for path in files)
    if total <= max_bytes:
        return []

    removed: list[Path] = []
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        if total <= max_bytes:
            break
        if keep and path in keep:
            continue
        size = path.stat().st_size
        with contextlib.suppress(OSError):
            path.unlink()
            removed.append(path)
            total -= size
    if removed:
        LOGGER.info(
            "Pruned %d file(s) from the clip cache to stay under %d MB",
            len(removed),
            max_bytes // (1024 * 1024),
        )
    return removed


class ClipCache:
    """Fetch-once storage for clips, keyed by the same id the listing hands out."""

    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self._locks: dict[str, asyncio.Lock] = {}
        # One Blink download at a time. prepare_download() makes the Sync
        # Module upload the clip and polls the cloud for it; several of those
        # in flight together is the pattern that gets an account rate-limited,
        # and a page of thumbnails would start dozens.
        self._download_slot = asyncio.Semaphore(1)

    def clip_path(self, key: str) -> Path:
        return self.root / f"{key}.mp4"

    def thumbnail_path(self, key: str) -> Path:
        return self.root / f"{key}.jpg"

    def metadata_path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    @staticmethod
    def _usable(path: Path) -> bool:
        return path.exists() and path.stat().st_size > 0

    def cached_clip(self, key: str) -> Path | None:
        """The clip file if it is here, touched so it counts as recently used."""
        path = self.clip_path(key)
        if not self._usable(path):
            return None
        with contextlib.suppress(OSError):
            os.utime(path, None)
        return path

    def cached_thumbnail(self, key: str) -> Path | None:
        path = self.thumbnail_path(key)
        return path if self._usable(path) else None

    def metadata(self, key: str) -> dict[str, Any]:
        """What the listing said about a cached clip, for filenames after a restart."""
        try:
            return json.loads(self.metadata_path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    async def ensure_clip(self, manager: ClipManager, clip: dict[str, Any]) -> Path:
        """Return the cached copy of a clip, fetching it from Blink first if needed."""
        key = clip_id(clip)
        target = self.clip_path(key)
        async with self._lock(key):
            cached = self.cached_clip(key)
            if cached is not None:
                return cached

            self.root.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(".mp4.part")
            async with self._download_slot:
                response = await manager.open_clip_response(clip)
                try:
                    with partial.open("wb") as handle:
                        async for chunk in response.content.iter_chunked(102400):
                            handle.write(chunk)
                finally:
                    response.close()

            if not self._usable(partial):
                with contextlib.suppress(FileNotFoundError):
                    partial.unlink()
                raise RuntimeError(f"Blink returned an empty clip for {key}")

            os.replace(partial, target)
            with contextlib.suppress(OSError):
                self.metadata_path(key).write_text(
                    json.dumps(printable_clip(clip)), encoding="utf-8"
                )
            self.prune(keep={target, self.metadata_path(key)})
            LOGGER.info(
                "Cached %s clip %s (%d bytes)",
                clip.get("source"),
                key,
                target.stat().st_size,
            )
            return target

    async def ensure_thumbnail(
        self,
        manager: ClipManager,
        clip: dict[str, Any],
        *,
        ffmpeg: str,
        loglevel: str,
    ) -> Path:
        """Return the clip's first frame as a JPEG, cutting it if it is not cached."""
        key = clip_id(clip)
        cached = self.cached_thumbnail(key)
        if cached is not None:
            return cached

        source = await self.ensure_clip(manager, clip)
        target = self.thumbnail_path(key)
        async with self._lock(f"{key}.jpg"):
            cached = self.cached_thumbnail(key)
            if cached is not None:
                return cached

            partial = target.with_suffix(".jpg.part")
            with contextlib.suppress(FileNotFoundError):
                partial.unlink()
            process = await asyncio.create_subprocess_exec(
                *thumbnail_args(ffmpeg, loglevel, source, partial),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await process.communicate()
            if process.returncode != 0 or not self._usable(partial):
                with contextlib.suppress(FileNotFoundError):
                    partial.unlink()
                detail = (stderr or b"").decode("utf-8", "replace").strip()[-800:]
                LOGGER.warning("ffmpeg could not cut a frame from %s: %s", source.name, detail)
                raise RuntimeError(f"Could not read a frame from clip {key}")

            os.replace(partial, target)
            return target

    def prune(self, keep: set[Path] | None = None) -> list[Path]:
        return prune_directory(self.root, self.max_bytes, keep)
