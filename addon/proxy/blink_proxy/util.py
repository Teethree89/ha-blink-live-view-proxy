"""Small formatting and timestamp helpers."""

from __future__ import annotations

import datetime
import urllib.parse
from typing import Any

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
