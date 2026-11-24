"""Cloud clips are listed from Blink's v4 media list.

Blink stopped returning new clips from GET /api/v1/accounts/{id}/media/changed,
the endpoint blinkpy 0.25.9 calls, while the Blink app (on POST
/api/v4/accounts/{id}/media) still showed them, so the viewer was empty (#92).
These tests stand in for Blink at blinkpy's HTTP functions and pin the v4
request, its pagination, the URL handling and the fallback. Run from the repo
root:

    python tests/test_cloud_clips.py
"""

from __future__ import annotations

import asyncio
import datetime
import pathlib
import sys
import urllib.parse
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "proxy"))

from blink_proxy import clips as clips_module  # noqa: E402
from blink_proxy.clips import ClipManager  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0
BASE = "https://rest-u011.immedia-semi.com"


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


class FakeCamera:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeBlink:
    def __init__(self) -> None:
        self.account_id = 1234
        self.urls = type("Urls", (), {"base_url": BASE})()
        self.cameras = {"Front Door": FakeCamera("Front Door"), "Garage": FakeCamera("Garage")}
        self.sync: dict[str, Any] = {}


class FakeClient:
    def __init__(self) -> None:
        self.blink = FakeBlink()

    def _require_blink(self) -> FakeBlink:
        return self.blink

    def camera_slug(self, camera: FakeCamera, name: str) -> str:
        return name.lower().replace(" ", "_")


class FakeApi:
    """Stands in for blinkpy.api: answers v4 POSTs from a list, logs calls."""

    def __init__(self, v4_pages: list[Any], v1_pages: list[Any] | None = None) -> None:
        self.v4_pages = list(v4_pages)
        self.v1_pages = list(v1_pages or [])
        self.posts: list[str] = []
        self.v1_calls = 0

    async def http_post(self, blink: Any, url: str, **_kwargs: Any) -> Any:
        self.posts.append(url)
        return self.v4_pages.pop(0) if self.v4_pages else {"media": []}

    async def request_videos(self, blink: Any, time: Any = None, page: int = 0) -> Any:
        self.v1_calls += 1
        return self.v1_pages.pop(0) if self.v1_pages else {"media": []}


def recent(minutes: int) -> str:
    when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def entry(camera: str, minutes: int, media: str | None, **extra: Any) -> dict[str, Any]:
    return {"device_name": camera, "created_at": recent(minutes), "media": media, **extra}


def run(fake: FakeApi, **kwargs: Any) -> list[dict[str, Any]]:
    original_post = clips_module.blink_api.http_post
    original_videos = clips_module.blink_api.request_videos
    clips_module.blink_api.http_post = fake.http_post
    clips_module.blink_api.request_videos = fake.request_videos
    try:
        manager = ClipManager(FakeClient())  # type: ignore[arg-type]
        return asyncio.run(manager.list_clips(source="cloud", **kwargs))
    finally:
        clips_module.blink_api.http_post = original_post
        clips_module.blink_api.request_videos = original_videos


def test_v4_list_is_used() -> None:
    print("\nclips come from the v4 media list")
    video = "/api/v4/accounts/1234/media/9/video/contents?sig=abc"
    fake = FakeApi(
        [
            {
                "media": [
                    entry("Front Door", 5, f"https://media.example.test{video}"),
                    entry("Garage", 10, None),  # a snapshot, no video
                    entry("Garage", 15, "/api/v4/accounts/1234/media/8/video", deleted=True),
                ],
                "pagination_key": None,
            }
        ]
    )
    clips = run(fake)
    check(len(fake.posts) == 1 and fake.v1_calls == 0, "one v4 POST, media/changed untouched")
    url = urllib.parse.urlsplit(fake.posts[0])
    check(
        f"{url.scheme}://{url.netloc}{url.path}" == f"{BASE}/api/v4/accounts/1234/media",
        "the POST goes to the account's v4 media list",
    )
    check("start_time" in urllib.parse.parse_qs(url.query), "it asks from the start of the window")
    check(len(clips) == 1, "entries without a video, and deleted ones, are dropped")
    check(
        clips and clips[0]["url"] == f"{BASE}{video}",
        "the clip URL keeps its path and query but uses the account's API host",
    )
    check(clips and clips[0]["slug"] == "front_door", "the clip is matched to its camera")


def test_pagination_follows_keys() -> None:
    print("\nthe v4 list is followed by pagination key")
    fake = FakeApi(
        [
            {"media": [entry("Garage", 1, "/a")], "pagination_key": "k1"},
            {"media": [entry("Garage", 2, None)], "pagination_key": "k2"},
            {"media": [entry("Garage", 3, "/b")], "pagination_key": "k3"},
            {"media": [entry("Garage", 4, "/c")], "pagination_key": None},
        ]
    )
    clips = run(fake, pages=2)
    keys = [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query).get("pagination_key") for u in fake.posts]
    check(keys == [None, ["k1"], ["k2"]], "each request carries the previous page's key")
    check(len(clips) == 2, "a page with no video does not count against the pages asked for")

    looping = FakeApi([{"media": [], "pagination_key": "same"}] * 50)
    check(run(looping) == [], "an empty list is an empty answer")
    check(len(looping.posts) == 2, "a key that does not move stops the walk")


def test_hostile_urls_are_dropped() -> None:
    print("\na listing cannot send the authenticated download elsewhere")
    fake = FakeApi(
        [
            {
                "media": [
                    entry("Garage", 1, "evil.test/x"),
                    entry("Garage", 2, "//evil.test/x"),
                    entry("Garage", 3, "http://evil.test/ok"),
                ]
            }
        ]
    )
    clips = run(fake)
    check(
        sorted(c["url"] for c in clips) == [f"{BASE}/ok", f"{BASE}/x"],
        "a bare host is dropped, and any other host is replaced by Blink's",
    )


def test_fallback_to_media_changed() -> None:
    print("\nmedia/changed is still asked when v4 does not answer")
    fake = FakeApi([None], v1_pages=[{"media": [entry("Front Door", 1, "/old")]}])
    clips = run(fake)
    check(fake.v1_calls >= 1, "the old endpoint is tried")
    check([c["url"] for c in clips] == [f"{BASE}/old"], "and its clips are listed")


def main() -> int:
    for test in (
        test_v4_list_is_used,
        test_pagination_follows_keys,
        test_hostile_urls_are_dropped,
        test_fallback_to_media_changed,
    ):
        test()

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\nfailed:")
        for name in FAILURES:
            print(f"  {name}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
