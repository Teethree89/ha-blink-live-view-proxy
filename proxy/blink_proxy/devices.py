"""Blink device state for Home Assistant entities, and the poll that keeps it fresh.

This is what lets a household drop the official Blink integration: the
cameras' motion switches, battery, temperature and motion state, the sync
module's arm state, and the thumbnail, all read from the session this proxy
already holds.

Why not just run the official integration alongside? Its blinkpy refreshes
tokens in Auth.startup() on every setup or reload and never writes the new
ones back to its config entry, so after about thirty days it is refreshing
from a dead token. It then falls back to a password sign-in, Blink texts a
2FA code for every one, and blinkpy cannot answer the SMS step. That was an
SMS storm and a rate-limit lockout on 2026-09-18. This proxy persists every
refresh through its auth-file callback and stores no password, so its
session renews itself.

The one rule that matters here: nothing in this module may ever start a
sign-in. Blink.start() and Auth.startup() are the only blinkpy paths that
fall through to the password flow, and neither is reachable from here.
Blink.refresh() and every request it makes go through Auth.query(), which
renews with the refresh-token grant and nothing else. When that fails, the
poll stops asking and says so. Recovery stays with the systemd watchdog,
which restarts the service at most three times in thirty minutes.

The poll runs only while something reads /devices. A proxy nobody asks makes
no more Blink calls than it did before this existed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any, Callable

from blinkpy.auth import (
    BlinkTwoFARequiredError,
    LoginError,
    TokenRefreshFailed,
    UnauthorizedError,
)

from .camera_controls import _answer_busy
from .constants import LOGGER_NAME

LOGGER = logging.getLogger(LOGGER_NAME)

POLL_DEFAULT_SECONDS = 300
POLL_MIN_SECONDS = 60
POLL_MAX_SECONDS = 3600
# The longest a failing poll waits before trying again. Each failure doubles
# the wait from the configured interval up to this.
BACKOFF_MAX_SECONDS = 3600
# With nobody reading /devices for this many intervals (and at least
# IDLE_MIN_SECONDS), the poll stops until the next read starts it again.
IDLE_INTERVALS = 3
IDLE_MIN_SECONDS = 900
# How often the loop wakes to notice that nobody is reading any more.
LOOP_TICK_SECONDS = 30
# After a snapshot, how many more times to fetch a thumbnail that has not
# changed yet, and how long to wait between. Seen on a Mini: the new address
# arrives before the image behind it can be downloaded.
SNAPSHOT_RETRIES = 3
SNAPSHOT_RETRY_SECONDS = 2

# What a failed token refresh surfaces as. Any of these means the session
# itself is in trouble, and asking again on a timer would only add to whatever
# rate limit caused it. BlinkTwoFARequiredError cannot come from a refresh,
# but if it ever does, the answer is the same: stop.
AUTH_ERRORS = (
    TokenRefreshFailed,
    UnauthorizedError,
    LoginError,
    BlinkTwoFARequiredError,
)


def clamp_interval(value: Any) -> int:
    """A poll interval inside the range this proxy will honour."""
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return POLL_DEFAULT_SECONDS
    return max(POLL_MIN_SECONDS, min(POLL_MAX_SECONDS, seconds))


def backoff_seconds(interval: int, failures: int) -> int:
    """How long to wait after `failures` consecutive failed polls."""
    if failures <= 0:
        return interval
    return min(BACKOFF_MAX_SECONDS, interval * (2 ** min(failures, 12)))


def _bool_or_none(value: Any) -> bool | None:
    # blinkpy keeps "unknown" in motion_enabled when Blink leaves it out.
    return value if isinstance(value, bool) else None


def _number_or_none(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _snapshot_id(camera: Any) -> str | None:
    """A short id that changes whenever the cached thumbnail does.

    A hash of the image itself, not of its URL. blinkpy records a new
    thumbnail URL even when downloading it fails, so a URL-based id announced
    a new picture while the old one was still being served. Home Assistant
    uses the id to know its copy is stale.
    """
    image = getattr(camera, "_cached_image", None)
    if not image:
        return None
    return hashlib.sha1(image, usedforsecurity=False).hexdigest()[:12]


def sync_armed(sync: Any) -> bool | None:
    """The sync module's armed state, read without blinkpy's side effects.

    BlinkSyncModule.arm marks the module unavailable when network_info is
    missing, which is not something a read should do.
    """
    try:
        armed = sync.network_info["network"]["armed"]
    except (KeyError, TypeError):
        return None
    return armed if isinstance(armed, bool) else None


def camera_row(client: Any, name: str, camera: Any) -> dict[str, Any]:
    """One camera's entity state, in the shape /devices returns."""
    slug = client.camera_slug(camera, name)
    battery_state = getattr(camera, "battery_state", None)
    temperature = getattr(camera, "temperature_calibrated", None)
    if _number_or_none(temperature) is None:
        temperature = getattr(camera, "temperature", None)
    sync = getattr(camera, "sync", None)
    return {
        "slug": slug,
        "name": name,
        "id": str(camera.camera_id),
        "serial": camera.serial,
        "network_id": str(camera.network_id),
        "sync_module": getattr(sync, "name", None),
        "product_type": getattr(camera, "product_type", None),
        "status": getattr(camera, "status", None),
        "motion_enabled": _bool_or_none(getattr(camera, "motion_enabled", None)),
        "motion_detected": bool(getattr(camera, "motion_detected", False)),
        "battery": battery_state,
        "battery_low": (
            None if battery_state is None else str(battery_state).lower() != "ok"
        ),
        "battery_level": _number_or_none(getattr(camera, "battery_level", None)),
        "battery_voltage": _number_or_none(getattr(camera, "battery_voltage", None)),
        # Fahrenheit, as Blink reports it and the official integration shows it.
        "temperature": _number_or_none(temperature),
        "wifi_strength": _number_or_none(getattr(camera, "wifi_strength", None)),
        "sync_signal_strength": _number_or_none(
            getattr(camera, "sync_signal_strength", None)
        ),
        "last_record": getattr(camera, "last_record", None),
        "snapshot_id": _snapshot_id(camera),
        "snapshot_url": f"/cameras/{slug}/snapshot.jpg",
    }


def sync_row(name: str, sync: Any) -> dict[str, Any]:
    """One sync module's state, in the shape /devices returns."""
    return {
        "name": name,
        "network_id": str(sync.network_id),
        "sync_id": str(getattr(sync, "sync_id", "") or "") or None,
        "serial": getattr(sync, "serial", None),
        "status": getattr(sync, "status", None),
        "armed": sync_armed(sync),
    }


def devices_payload(client: Any) -> dict[str, Any]:
    """Everything Home Assistant builds its Blink entities from.

    Read from blinkpy's memory, so answering this never contacts Blink.
    """
    blink = client._require_blink()
    cameras = [
        camera_row(client, name, camera) for name, camera in blink.cameras.items()
    ]
    cameras.sort(key=lambda row: row["slug"])
    syncs = [sync_row(name, sync) for name, sync in blink.sync.items()]
    syncs.sort(key=lambda row: row["network_id"])
    return {"cameras": cameras, "sync_modules": syncs}


def _drop_cached_videos(blink: Any) -> None:
    """Keep blinkpy from holding every camera's last clip in memory.

    BlinkCamera.update_images downloads the latest clip whenever its cache is
    None, and the proxy never reads it: clips are served by ClipManager. An
    empty cache is not None, so a clip is fetched only when motion is actually
    detected, and dropped again here. This host is a TV box, not a server.
    """
    for camera in blink.cameras.values():
        camera._cached_video = b""


async def refresh_blink(blink: Any) -> None:
    """One full Blink refresh: sync modules, cameras, motion and thumbnails.

    Blink.refresh() swallows most request failures and carries on with what it
    had, so a refresh that produced nothing is treated as a failure here
    rather than reported as fresh data.
    """
    _drop_cached_videos(blink)
    try:
        # Unforced, so check_new_videos measures motion from the last refresh
        # and thumbnails are downloaded only when they have changed. The proxy
        # builds Blink with refresh_rate=60, below the minimum interval, so
        # the throttle inside never skips a scheduled poll.
        await blink.refresh()
    finally:
        _drop_cached_videos(blink)
    if blink.sync and not any(
        getattr(sync, "available", False) for sync in blink.sync.values()
    ):
        raise RuntimeError("Blink answered, but no sync module refreshed")


class DevicePoller:
    """Refreshes Blink on an interval while Home Assistant is reading it.

    One poll at a time, and actions wait for it: a token refresh racing a
    second one could present a refresh token the first had already rotated.
    """

    def __init__(
        self,
        get_client: Callable[[], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._get_client = get_client
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self.lock = asyncio.Lock()
        self.interval = POLL_DEFAULT_SECONDS
        self.state = "idle"
        self.failures = 0
        self.polls = 0
        self.last_attempt: float | None = None
        self.last_success: float | None = None
        self.last_error: str | None = None
        self._next_at = 0.0
        self._last_demand: float | None = None
        self._task: asyncio.Task[None] | None = None
        # The client whose token refresh failed. Its session will not recover
        # by being asked again; a new login or a restart replaces the client,
        # and only then does polling resume.
        self._failed_client: Any = None

    def status(self) -> dict[str, Any]:
        now = self._clock()
        return {
            "state": self.state,
            "interval": self.interval,
            "failures": self.failures,
            "polls": self.polls,
            "last_attempt": self.last_attempt,
            "last_success": self.last_success,
            "last_error": self.last_error,
            "next_in": (
                max(0, round(self._next_at - now))
                if self._task is not None and not self._task.done()
                else None
            ),
        }

    def demand(self, interval: Any = None) -> None:
        """Record a reader, and start the poll if it is not running."""
        if interval is not None:
            new_interval = clamp_interval(interval)
            if new_interval < self.interval and self.failures == 0:
                # Honour a shorter interval now rather than after the old one.
                self._next_at = min(self._next_at, self._clock() + new_interval)
            self.interval = new_interval
        self._last_demand = self._clock()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="blink-device-poll")

    def _idle_after(self) -> float:
        return max(IDLE_MIN_SECONDS, self.interval * IDLE_INTERVALS)

    async def _run(self) -> None:
        LOGGER.info("Blink device poll started, every %d s", self.interval)
        while True:
            now = self._clock()
            if self._last_demand is None or now - self._last_demand > self._idle_after():
                self.state = "idle"
                LOGGER.info("Blink device poll stopped: nothing has read it recently")
                return
            if now >= self._next_at:
                await self.poll_once()
            wait = min(LOOP_TICK_SECONDS, max(1.0, self._next_at - self._clock()))
            await self._sleep(wait)

    async def poll_once(self) -> None:
        """Refresh Blink once, and schedule the next attempt."""
        client = self._get_client()
        if client is None or not getattr(client, "ready", False):
            self.state = "not_ready"
            self._next_at = self._clock() + self.interval
            return
        if client is self._failed_client:
            # Already reported. Stay quiet until the client is replaced.
            self._next_at = self._clock() + BACKOFF_MAX_SECONDS
            return

        self.last_attempt = self._wall()
        async with self.lock:
            try:
                await refresh_blink(client.blink)
            except AUTH_ERRORS as err:
                self._failed_client = client
                self.failures += 1
                self.state = "auth_failed"
                self.last_error = f"{type(err).__name__}: {err}".rstrip(": ")
                self._next_at = self._clock() + BACKOFF_MAX_SECONDS
                LOGGER.error(
                    "Blink device poll: the token refresh failed (%s). Polling "
                    "stops here and will not sign in; it resumes once the "
                    "session is replaced by a restart or a new login.",
                    self.last_error,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - reported, then backed off
                self.failures += 1
                delay = backoff_seconds(self.interval, self.failures)
                self.state = "backoff"
                self.last_error = f"{type(err).__name__}: {err}".rstrip(": ")
                self._next_at = self._clock() + delay
                LOGGER.warning(
                    "Blink device poll failed (%s), attempt %d; next try in %d s",
                    self.last_error,
                    self.failures,
                    delay,
                )
                return

        self.failures = 0
        self.polls += 1
        self.state = "ok"
        self.last_error = None
        self.last_success = self._wall()
        self._next_at = self._clock() + self.interval
        LOGGER.debug("Blink device poll %d complete", self.polls)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


async def refresh_camera(blink: Any, camera: Any) -> None:
    """Re-read one camera after acting on it, instead of refreshing everything.

    The homescreen comes first because Minis and doorbells take their state
    from it, not from the per-camera endpoint.
    """
    await blink.get_homescreen()
    sync = camera.sync
    info = await sync.get_camera_info(
        camera.camera_id, unique_info=sync.get_unique_info(camera.name)
    )
    _drop_cached_videos(blink)
    try:
        await camera.update(info, force_cache=False, expire_clips=False)
    finally:
        _drop_cached_videos(blink)


class ActionError(Exception):
    """Blink did not accept a command."""


class BlinkBusyError(ActionError):
    """Blink answered 307: the camera is mid-command or streaming a live view.

    Worth telling apart from a refusal, because only this one is worth
    retrying, and only once the live view has ended. Blink holds a camera
    busy for as long as any client, the app included, has a live view open on
    it - measured for the Camera Controls sheet, and the reason a Mini's
    motion change was dropped during this feature's own live test.
    """


def _raise_if_busy(response: Any, what: str) -> None:
    if _answer_busy(response):
        raise BlinkBusyError(
            f"{what}: the camera is busy, usually because a live view is open "
            "on it. Try again once it has ended."
        )


async def set_motion_detection(client: Any, poller: DevicePoller, slug: str, enabled: bool) -> dict[str, Any]:
    camera = client.camera_for_slug(slug)
    async with poller.lock:
        response = await camera.async_arm(bool(enabled))
        _raise_if_busy(response, f"Motion detection on {slug} was not changed")
        if not response:
            raise ActionError(f"Blink did not accept the motion change for {slug}")
        await refresh_camera(client._require_blink(), camera)
    # Blink can answer a command it then does not apply - seen on a Mini
    # whose live view had just ended. Saying so beats reporting success
    # while the switch quietly stays where it was.
    if _bool_or_none(camera.motion_enabled) is not bool(enabled):
        raise ActionError(
            f"Blink accepted the motion change for {slug} but did not apply it; "
            "the camera may be busy. Try again in a minute."
        )
    return camera_row(client, camera.name, camera)


async def snap_picture(client: Any, poller: DevicePoller, slug: str) -> dict[str, Any]:
    camera = client.camera_for_slug(slug)
    before = camera._cached_image
    async with poller.lock:
        response = await camera.snap_picture()
        _raise_if_busy(response, f"No new picture was taken on {slug}")
        if not response:
            raise ActionError(f"Blink did not take a new picture for {slug}")
        # snap_picture waits for the command, then downloads the thumbnail it
        # already knew about. The new one's address arrives with the camera's
        # next state, so read that before answering.
        await refresh_camera(client._require_blink(), camera)
        for _ in range(SNAPSHOT_RETRIES):
            if camera._cached_image and camera._cached_image != before:
                break
            await asyncio.sleep(SNAPSHOT_RETRY_SECONDS)
            media = await camera.get_media()
            if media is not None and getattr(media, "status", None) == 200:
                camera._cached_image = await media.read()
    if not camera._cached_image or camera._cached_image == before:
        raise ActionError(
            f"Blink took a picture on {slug}, but the new image could not be "
            "downloaded yet. Try again shortly."
        )
    return camera_row(client, camera.name, camera)


def sync_for_network(client: Any, network_id: str) -> tuple[str, Any]:
    blink = client._require_blink()
    for name, sync in blink.sync.items():
        if str(sync.network_id) == str(network_id):
            return name, sync
    raise KeyError(network_id)


async def set_sync_armed(client: Any, poller: DevicePoller, network_id: str, armed: bool) -> dict[str, Any]:
    name, sync = sync_for_network(client, network_id)
    async with poller.lock:
        response = await sync.async_arm(bool(armed))
        _raise_if_busy(response, f"{name} was not {'armed' if armed else 'disarmed'}")
        if not response:
            raise ActionError(f"Blink did not accept the arm change for {name}")
        await sync.get_network_info()
    if sync_armed(sync) is not bool(armed):
        raise ActionError(
            f"Blink accepted the arm change for {name} but did not apply it. "
            "Try again in a minute."
        )
    return sync_row(name, sync)
