"""Blink cloud and Sync Module local-storage clip helpers."""

from __future__ import annotations

import datetime
import hashlib
import logging
import urllib.parse
from pathlib import Path
from typing import Any

from blinkpy import api as blink_api
from blinkpy.helpers.util import get_time

from .blink import BlinkClient
from .constants import LOGGER_NAME
from .util import clip_filename, normalize_slug, parse_blink_time

LOGGER = logging.getLogger(LOGGER_NAME)

# How many v4 clip-list pages without a single video may be skipped in a row.
V4_MAX_EMPTY_PAGES = 10


def _media_path(url: Any) -> str | None:
    """The path of a Blink media URL, or None if it is not a usable one.

    The v4 list hands back absolute URLs, where media/changed gave a path. The
    host is dropped and the account's own API host used instead, so a listing
    can never point a download, which carries the Blink auth header, at
    somewhere else.
    """
    if not isinstance(url, str) or not url:
        return None
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.urlunsplit(("", "", parts.path, parts.query, ""))
    return path if path.startswith("/") and not path.startswith("//") else None

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

        for item in await self._cloud_media(since, pages):
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

    async def _cloud_media(
        self, since: datetime.datetime, pages: int
    ) -> list[dict[str, Any]]:
        """Blink's cloud media entries since a time, newest list first.

        Blink stopped answering ``GET /api/v1/accounts/{id}/media/changed``
        with new clips: it returns an empty list while the Blink app, which
        moved to ``POST /api/v4/accounts/{id}/media``, shows them (#92;
        fronzbot/blinkpy#1353). blinkpy 0.25.9 still calls the old one, so
        this asks the v4 list itself and only falls back to the old endpoint
        when v4 gives no usable answer.

        TEMPORARY: a local port of fronzbot/blinkpy#1339, which was unmerged
        when this shipped. Once a blinkpy release carries the v4 clip list
        (``request_clips``) and requirements.txt is bumped to it, replace
        ``_v4_cloud_media``/``_v1_cloud_media`` with that call and delete
        tests/test_cloud_clips.py's fakes for them. Tracked in #93.
        """
        media = await self._v4_cloud_media(since, pages)
        if media is not None:
            return media
        LOGGER.warning(
            "Blink's v4 clip list gave no usable answer; "
            "falling back to the older media/changed list"
        )
        return await self._v1_cloud_media(since, pages)

    async def _v4_cloud_media(
        self, since: datetime.datetime, pages: int
    ) -> list[dict[str, Any]] | None:
        """Follow the v4 list's pagination keys. None if v4 did not answer."""
        blink = self.client._require_blink()
        media: list[dict[str, Any]] = []
        pagination_key: str | None = None
        pages_with_clips = 0
        # A page can hold only snapshot entries with no video; skipping a few
        # of those must not count against the pages asked for, but it is
        # still bounded so a misbehaving key cannot loop for ever.
        for _request in range(pages + V4_MAX_EMPTY_PAGES):
            query = {"start_time": get_time(since.timestamp())}
            if pagination_key:
                query["pagination_key"] = pagination_key
            url = (
                f"{blink.urls.base_url}/api/v4/accounts/{blink.account_id}"
                f"/media?{urllib.parse.urlencode(query)}"
            )
            response = await blink_api.http_post(blink, url=url)
            if not isinstance(response, dict) or not isinstance(
                response.get("media"), list
            ):
                # The first page failing means v4 is not usable here; a later
                # one failing keeps what was already listed.
                return None if pagination_key is None else media

            page_clips = []
            for entry in response["media"]:
                if not isinstance(entry, dict):
                    continue
                path = _media_path(entry.get("media"))
                if path is None:
                    continue
                page_clips.append({**entry, "media": path})
            media.extend(page_clips)
            if page_clips:
                pages_with_clips += 1
                if pages_with_clips >= pages:
                    break

            next_key = response.get("pagination_key")
            if not next_key or next_key == pagination_key:
                break
            pagination_key = str(next_key)
        return media

    async def _v1_cloud_media(
        self, since: datetime.datetime, pages: int
    ) -> list[dict[str, Any]]:
        blink = self.client._require_blink()
        media: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            response = await blink_api.request_videos(
                blink, time=since.timestamp(), page=page
            )
            items = response.get("media", []) if isinstance(response, dict) else []
            if not items:
                break
            media.extend(item for item in items if isinstance(item, dict))
        return media

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

def clip_thumbnail_url(
    clip: dict[str, Any],
    *,
    hours: float,
    pages: int,
    limit: int,
) -> str:
    """The first frame of a listed clip, on the same query the download uses."""
    return clip_download_url(clip, hours=hours, pages=pages, limit=limit).replace(
        ".mp4?", ".jpg?", 1
    )

def printable_clip(
    clip: dict[str, Any],
    download_url: str | None = None,
    thumbnail_url: str | None = None,
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
    if thumbnail_url:
        row["thumbnail_url"] = thumbnail_url
    return row
