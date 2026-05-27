"""Blink cloud and Sync Module local-storage clip helpers."""

from __future__ import annotations

import datetime
import hashlib
import logging
import urllib.parse
from pathlib import Path
from typing import Any

from blinkpy import api as blink_api

from .blink import BlinkClient
from .constants import LOGGER_NAME
from .util import clip_filename, normalize_slug, parse_blink_time

LOGGER = logging.getLogger(LOGGER_NAME)

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
