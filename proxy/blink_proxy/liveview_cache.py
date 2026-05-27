"""Cached last-live-view helpers."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from .constants import LOGGER_NAME
from .util import liveview_glob

LOGGER = logging.getLogger(LOGGER_NAME)

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
