"""HTTP routes for the Blink live-view proxy."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import os
import platform
import secrets
import shutil
import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from aiohttp import web

from . import selfupdate
from .auth import (
    check_auth_control_authorized,
    check_authorized,
    check_header_authorized,
    is_authorized,
    rewrite_playlist_for_token,
)
from .auth_flow import (
    AuthConflictError,
    AuthenticationController,
    AuthFlowError,
    StaleChallengeError,
)
from .blink import BlinkStreamBroker, LiveViewHandle, _wait_for_pin
from .clips import ClipManager, clip_download_url, clip_filename, clip_id, printable_clip
from .config import resolve_path
from .constants import LOGGER_NAME, PROXY_VERSION
from .hls import HlsManager
from .liveview_cache import ensure_last_liveview_mp4, find_last_liveview
from .ptt import liveview_session_key, ptt_handler
from .util import liveview_filename

LOGGER = logging.getLogger(LOGGER_NAME)

AUTH_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

def _require_client(request: web.Request):
    client = request.app.get("client")
    if client is None or not getattr(client, "ready", False):
        raise web.HTTPServiceUnavailable(text="Blink authentication is not ready\n")
    return client

async def _auth_json_body(request: web.Request) -> dict[str, Any]:
    if request.content_length is not None and request.content_length > 4096:
        raise web.HTTPRequestEntityTooLarge(
            max_size=4096, actual_size=request.content_length
        )
    try:
        body = await request.json()
    except Exception as err:  # noqa: BLE001 - never reflect parser/request details
        raise web.HTTPBadRequest(text="Invalid authentication request\n") from err
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="Invalid authentication request\n")
    return body

def _auth_response(controller: AuthenticationController, *, status: int = 200):
    return web.json_response(
        controller.status(), status=status, headers=AUTH_RESPONSE_HEADERS
    )

async def auth_status_handler(request: web.Request) -> web.Response:
    check_auth_control_authorized(request)
    return _auth_response(request.app["auth_controller"])

async def auth_login_handler(request: web.Request) -> web.Response:
    check_auth_control_authorized(request)
    body = await _auth_json_body(request)
    controller: AuthenticationController = request.app["auth_controller"]
    try:
        await controller.start_browser_login(
            str(body.get("username") or ""), str(body.get("password") or "")
        )
    except AuthConflictError as err:
        raise web.HTTPConflict(
            text="A Blink authentication attempt is already active\n",
            headers=AUTH_RESPONSE_HEADERS,
        ) from err
    except AuthFlowError as err:
        raise web.HTTPBadRequest(
            text="Blink username and password are required\n",
            headers=AUTH_RESPONSE_HEADERS,
        ) from err
    return _auth_response(controller, status=202)

async def auth_pin_handler(request: web.Request) -> web.Response:
    check_auth_control_authorized(request)
    body = await _auth_json_body(request)
    controller: AuthenticationController = request.app["auth_controller"]
    try:
        await controller.submit_pin(
            str(body.get("challenge_id") or ""), str(body.get("pin") or "")
        )
    except StaleChallengeError as err:
        raise web.HTTPConflict(
            text="The Blink challenge is stale or no longer active\n",
            headers=AUTH_RESPONSE_HEADERS,
        ) from err
    except AuthFlowError as err:
        raise web.HTTPBadRequest(
            text="Enter the newly issued numeric Blink PIN\n",
            headers=AUTH_RESPONSE_HEADERS,
        ) from err
    return _auth_response(controller, status=202)

async def auth_cancel_handler(request: web.Request) -> web.Response:
    check_auth_control_authorized(request)
    body = await _auth_json_body(request)
    controller: AuthenticationController = request.app["auth_controller"]
    try:
        await controller.cancel(str(body.get("challenge_id") or ""))
    except StaleChallengeError as err:
        raise web.HTTPConflict(
            text="The Blink challenge is stale or no longer active\n",
            headers=AUTH_RESPONSE_HEADERS,
        ) from err
    return _auth_response(controller)

async def index_handler(request: web.Request) -> web.Response:
    check_authorized(request)
    rows = _require_client(request).list_cameras()
    return web.json_response(
        {
            "service": "blink-liveview-proxy",
            "cameras": rows,
            "notes": [
                "HLS: /cameras/{slug}/hls/index.m3u8",
                "MPEG-TS: /cameras/{slug}/mpegts",
                "Last live view info: /cameras/{slug}/last-liveview",
                "Last live view download: /cameras/{slug}/last-liveview.ts",
                "Last live view MP4 download: /cameras/{slug}/last-liveview.mp4",
                "Recent clips: /clips?source=local&hours=24&limit=20",
            ],
        }
    )

async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})

WATCHDOG_STATE_FILE = Path("/var/lib/blink-liveview-proxy/watchdog-state")
PROCESS_STARTED_AT = time.time()

def _read_watchdog_state() -> dict[str, int]:
    try:
        text = WATCHDOG_STATE_FILE.read_text()
    except OSError:
        return {}
    state: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key in ("last_restart", "attempts"):
            try:
                state[key] = int(value)
            except ValueError:
                pass
    return state

def _environment(config: dict[str, Any]) -> dict[str, Any]:
    """What this proxy is actually running on, for the dashboard's checks.

    Three answers to three questions the integration cannot ask from the other
    side of an HTTP boundary: which Python is running it, which blinkpy it
    imports, and whether ffmpeg is really where the config says. Every one of
    those fails as something else - a login that reports bad credentials, a
    live view that opens and never produces a frame - so naming them is worth
    the field.

    Read on each request rather than cached: `ffmpeg` is a config value, and
    someone who installs it while the proxy runs should see the dashboard clear
    on the next poll instead of after a restart.
    """
    try:
        blinkpy = package_version("blinkpy")
    except PackageNotFoundError:
        # Importable but not installed as a distribution - a source checkout on
        # PYTHONPATH. The version is genuinely unknown, so do not invent one.
        blinkpy = None
    return {
        "python": platform.python_version(),
        "blinkpy": blinkpy,
        "ffmpeg": shutil.which(str(config.get("ffmpeg") or "ffmpeg")),
    }

async def status_handler(request: web.Request) -> web.Response:
    controller: AuthenticationController = request.app["auth_controller"]
    client = request.app.get("client")
    client_status = (
        client.status()
        if client is not None
        else {
            "ready": False,
            "cameras_discovered": 0,
            "cameras_configured": len(request.app["config"].get("cameras", {})),
            "token_expiration": None,
        }
    )
    watchdog = _read_watchdog_state()
    now = time.time()
    expiration = client_status["token_expiration"]
    authorized = is_authorized(request)
    token_configured = bool(request.app.get("proxy_token"))
    return web.json_response(
        {
            **client_status,
            # A configured token hides these from strangers on the LAN. A
            # tokenless proxy has no private caller, so version keeps its
            # legacy public behavior. The environment goes with it: library
            # versions and binary paths are exactly what someone probing the
            # host would want. Update capability is stricter still: /update
            # refuses to run without a bearer token, so advertising a Fix
            # button in that state would promise an action that cannot work.
            **(
                {
                    "version": PROXY_VERSION,
                    "environment": _environment(request.app["config"]),
                }
                if authorized
                else {}
            ),
            **(
                {"update": selfupdate.describe()}
                if token_configured and authorized
                else {}
            ),
            "token_seconds_remaining": (expiration - now) if expiration else None,
            "process_started_at": PROCESS_STARTED_AT,
            "process_uptime_seconds": now - PROCESS_STARTED_AT,
            "watchdog_last_restart": watchdog.get("last_restart") or None,
            "watchdog_attempts": watchdog.get("attempts", 0),
            "auth_state": controller.state,
        }
    )

async def update_handler(request: web.Request) -> web.Response:
    """Start this install's own updater. Header-authenticated, and body-free.

    check_header_authorized, not check_authorized: a URL that upgrades and
    restarts the proxy must not be something a query token can carry into
    browser history, a proxy log, or a pasted link.

    The request body is never read. What gets installed is fixed on this host,
    so there is nothing here for a caller to choose - see selfupdate.start().
    """
    check_header_authorized(request, "Self-update")
    try:
        started = await selfupdate.start()
    except selfupdate.UpdateBusyError as err:
        raise web.HTTPConflict(text=f"{err}\n") from err
    except selfupdate.UpdateUnavailableError as err:
        raise web.HTTPNotImplemented(text=f"{err}\n") from err
    return web.json_response(started, status=202, headers=AUTH_RESPONSE_HEADERS)

async def cameras_handler(request: web.Request) -> web.Response:
    check_authorized(request)
    return web.json_response({"cameras": _require_client(request).list_cameras()})

def _clamped_float(value: str | None, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))

def _clamped_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))

async def clips_handler(request: web.Request) -> web.Response:
    check_authorized(request)
    source = request.query.get("source", "local")
    if source not in {"both", "cloud", "local"}:
        raise web.HTTPBadRequest(text="source must be one of both, cloud, or local\n")

    camera_slug = request.query.get("camera") or None
    hours = _clamped_float(request.query.get("hours"), 24, 0.1, 24 * 30)
    pages = _clamped_int(request.query.get("pages"), 3, 1, 10)
    limit = _clamped_int(request.query.get("limit"), 20, 1, 100)

    manager = ClipManager(_require_client(request))
    clips = await manager.list_clips(
        source=source,
        camera_slug=camera_slug,
        hours=hours,
        pages=pages,
        limit=limit,
    )
    return web.json_response(
        {
            "count": len(clips),
            "source": source,
            "camera": camera_slug,
            "hours": hours,
            "clips": [
                printable_clip(
                    clip,
                    download_url=clip_download_url(
                        clip,
                        hours=hours,
                        pages=pages,
                        limit=max(limit, 100),
                    ),
                )
                for clip in clips
            ],
        }
    )

async def clip_download_handler(request: web.Request) -> web.Response:
    check_authorized(request)
    clip_key = request.match_info["clip_id"]
    source = request.query.get("source", "local")
    if source not in {"cloud", "local"}:
        raise web.HTTPBadRequest(text="source must be cloud or local\n")

    camera_slug = request.query.get("camera") or None
    hours = _clamped_float(request.query.get("hours"), 24, 0.1, 24 * 30)
    pages = _clamped_int(request.query.get("pages"), 3, 1, 10)
    limit = _clamped_int(request.query.get("limit"), 200, 1, 500)

    manager = ClipManager(_require_client(request))
    clips = await manager.list_clips(
        source=source,
        camera_slug=camera_slug,
        hours=hours,
        pages=pages,
        limit=limit,
    )
    clip = next((item for item in clips if clip_id(item) == clip_key), None)
    if clip is None:
        raise web.HTTPNotFound(text="Clip was not found in the current manifest\n")

    try:
        upstream = await manager.open_clip_response(clip)
    except RuntimeError as err:
        raise web.HTTPBadGateway(text=f"{err}\n") from err

    filename = clip_filename(clip["camera_name"], clip["created_at"], clip["source"])
    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Accel-Buffering": "no",
    }
    content_length = upstream.headers.get("Content-Length")
    if content_length:
        headers["Content-Length"] = content_length

    response = web.StreamResponse(status=200, headers=headers)
    response.content_type = upstream.headers.get("Content-Type", "video/mp4")
    try:
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(102400):
            await response.write(chunk)
    except (ConnectionResetError, TimeoutError):
        LOGGER.debug("Browser clip download closed for %s", clip_key)
    finally:
        upstream.close()
    return response

async def mpegts_handler(request: web.Request) -> web.StreamResponse:
    check_authorized(request)
    _require_client(request)
    slug = request.match_info["slug"]
    broker: BlinkStreamBroker = request.app["broker"]
    session = request.query.get("session") or secrets.token_urlsafe(16)
    active_key = liveview_session_key(slug, session)
    force = request.query.get("force", "").lower() in ("1", "true", "yes")
    max_seconds = float(
        request.query.get(
            "seconds",
            request.app["config"].get("mpegts_session_seconds", 60),
        )
        or 0
    )
    cooldowns: dict[str, float] = request.app["mpegts_cooldowns"]
    now = time.monotonic()
    cooldown_until = cooldowns.get(slug, 0)
    if not force and cooldown_until > now:
        retry_after = max(1, int(cooldown_until - now))
        raise web.HTTPTooManyRequests(
            text=f"Live view cooldown active for {slug}; retry in {retry_after}s\n",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        liveview = await broker.start_liveview(slug)
    except KeyError as exc:
        raise web.HTTPNotFound(text=f"Unknown camera slug: {slug}\n") from exc
    request.app["active_liveviews"][active_key] = liveview
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    cache_handle: Any | None = None
    cache_tmp: Path | None = None
    cache_final: Path | None = None
    started_at = datetime.datetime.now(datetime.timezone.utc)
    bytes_written = 0

    if bool(request.app["config"].get("save_liveview_cache", True)):
        cache_root: Path = request.app["liveview_cache_dir"]
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_final = cache_root / liveview_filename(slug, started_at)
        cache_tmp = cache_final.with_suffix(".ts.part")
        cache_handle = cache_tmp.open("wb")

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "video/mp2t",
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    try:
        reader, writer = await asyncio.open_connection(liveview.host, liveview.port)
        deadline = time.monotonic() + max_seconds if max_seconds > 0 else None
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    LOGGER.info(
                        "MPEG-TS session reached %.0fs limit for %s",
                        max_seconds,
                        slug,
                    )
                    break
                chunk = await asyncio.wait_for(
                    reader.read(188 * 64),
                    timeout=remaining,
                )
            else:
                chunk = await reader.read(188 * 64)
            if not chunk:
                break
            if cache_handle is not None:
                cache_handle.write(chunk)
                bytes_written += len(chunk)
            await response.write(chunk)
    except asyncio.TimeoutError:
        LOGGER.info("MPEG-TS read timeout at session limit for %s", slug)
    except (ConnectionResetError, asyncio.CancelledError, BrokenPipeError):
        LOGGER.info("MPEG-TS client disconnected for %s", slug)
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if cache_handle is not None:
            cache_handle.close()
        if cache_tmp is not None and cache_final is not None:
            if bytes_written > 0:
                previous = request.app["last_liveviews"].get(slug, {})
                previous_path = previous.get("path")
                if previous_path:
                    with contextlib.suppress(FileNotFoundError):
                        Path(previous_path).unlink()
                    with contextlib.suppress(FileNotFoundError):
                        Path(previous_path).with_suffix(".mp4").unlink()
                os.replace(cache_tmp, cache_final)
                ended_at = datetime.datetime.now(datetime.timezone.utc)
                request.app["last_liveviews"][slug] = {
                    "slug": slug,
                    "path": str(cache_final),
                    "filename": cache_final.name,
                    "started_at": started_at.isoformat(),
                    "ended_at": ended_at.isoformat(),
                    "duration": (ended_at - started_at).total_seconds(),
                    "bytes": bytes_written,
                }
                LOGGER.info(
                    "Saved last live-view cache for %s to %s (%d bytes)",
                    slug,
                    cache_final,
                    bytes_written,
                )
            else:
                with contextlib.suppress(FileNotFoundError):
                    cache_tmp.unlink()
        cooldown_seconds = float(
            request.app["config"].get("mpegts_cooldown_seconds", 30) or 0
        )
        if cooldown_seconds > 0:
            cooldowns[slug] = time.monotonic() + cooldown_seconds
        if request.app["active_liveviews"].get(active_key) is liveview:
            request.app["active_liveviews"].pop(active_key, None)
        await liveview.close()
        with contextlib.suppress(Exception):
            await response.write_eof()
    return response

async def last_liveview_info_handler(request: web.Request) -> web.Response:
    check_authorized(request)
    slug = request.match_info["slug"]
    last = find_last_liveview(request.app, slug)
    cooldown_until = request.app["mpegts_cooldowns"].get(slug, 0)
    cooldown_remaining = max(0, int(cooldown_until - time.monotonic()))
    if not last:
        return web.json_response(
            {
                "available": False,
                "slug": slug,
                "cooldown_remaining": cooldown_remaining,
            }
        )

    payload = {
        key: value for key, value in last.items() if key != "path"
    }
    payload.update(
        {
            "available": Path(last["path"]).exists(),
            "download_url": f"/cameras/{slug}/last-liveview.ts",
            "mp4_download_url": f"/cameras/{slug}/last-liveview.mp4",
            "cooldown_remaining": cooldown_remaining,
        }
    )
    return web.json_response(payload)

async def last_liveview_download_handler(request: web.Request) -> web.FileResponse:
    check_authorized(request)
    slug = request.match_info["slug"]
    last = find_last_liveview(request.app, slug)
    if not last:
        raise web.HTTPNotFound(text=f"No cached live view for {slug}\n")
    path = Path(last["path"])
    return web.FileResponse(
        path,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{path.name}"',
        },
    )

async def last_liveview_mp4_download_handler(request: web.Request) -> web.FileResponse:
    check_authorized(request)
    slug = request.match_info["slug"]
    last = find_last_liveview(request.app, slug)
    if not last:
        raise web.HTTPNotFound(text=f"No cached live view for {slug}\n")
    ts_path = Path(last["path"])
    mp4_path = await ensure_last_liveview_mp4(request.app, slug, ts_path)
    return web.FileResponse(
        mp4_path,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{mp4_path.name}"',
        },
    )

async def hls_playlist_handler(request: web.Request) -> web.Response:
    check_authorized(request)
    _require_client(request)
    slug = request.match_info["slug"]
    browser_session = request.query.get("session") or ""
    manager: HlsManager = request.app["hls_manager"]
    try:
        session = await manager.get_or_start(slug)
    except KeyError as exc:
        raise web.HTTPNotFound(text=f"Unknown camera slug: {slug}\n") from exc
    await session.wait_ready()
    if browser_session:
        session.register_liveview_key(liveview_session_key(slug, browser_session))

    text = session.playlist.read_text(encoding="utf-8")
    token = request.query.get("token") if request.app.get("proxy_token") else None
    text = rewrite_playlist_for_token(text, token)
    return web.Response(
        text=text,
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )

async def hls_segment_handler(request: web.Request) -> web.FileResponse:
    check_authorized(request)
    _require_client(request)
    slug = request.match_info["slug"]
    filename = request.match_info["filename"]
    if "/" in filename or not filename.endswith(".ts"):
        raise web.HTTPNotFound()

    manager: HlsManager = request.app["hls_manager"]
    session = await manager.get_existing(slug)
    if session is None:
        raise web.HTTPNotFound(text="No active HLS session\n")

    path = session.directory / filename
    if not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})

async def make_app(
    config: dict[str, Any], config_base: Path, pin: str | None
) -> web.Application:
    # Open the HTTP control plane before login so a human can complete 2FA.
    # Cached/env/add-on authentication starts in the app lifecycle below.
    broker = BlinkStreamBroker(None)
    active_liveviews: dict[str, LiveViewHandle] = {}
    hls_manager = HlsManager(broker, config, config_base, active_liveviews)
    proxy_token = os.getenv(config["proxy_token_env"], config.get("proxy_token", ""))

    app = web.Application(client_max_size=4096)
    app["client"] = None
    app["broker"] = broker
    app["hls_manager"] = hls_manager
    app["proxy_token"] = proxy_token
    app["config"] = config
    app["liveview_cache_dir"] = resolve_path(config["liveview_cache_dir"], config_base)
    app["last_liveviews"] = {}
    app["mpegts_cooldowns"] = {}
    app["active_liveviews"] = active_liveviews
    app["mp4_locks"] = {}

    def activate_client(client) -> None:
        app["client"] = client
        broker.client = client

    auth_controller = AuthenticationController(
        config,
        config_base,
        startup_pin=pin,
        legacy_pin_waiter=_wait_for_pin,
        on_client=activate_client,
    )
    app["auth_controller"] = auth_controller

    app.router.add_get("/", index_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/status", status_handler)
    app.router.add_get("/auth/status", auth_status_handler)
    app.router.add_post("/auth/login", auth_login_handler)
    app.router.add_post("/auth/pin", auth_pin_handler)
    app.router.add_post("/auth/cancel", auth_cancel_handler)
    app.router.add_post("/update", update_handler)
    app.router.add_get("/cameras", cameras_handler)
    app.router.add_get("/clips", clips_handler)
    app.router.add_get("/clips/{clip_id}.mp4", clip_download_handler)
    app.router.add_get("/cameras/{slug}/mpegts", mpegts_handler)
    app.router.add_get("/cameras/{slug}/ptt", ptt_handler)
    app.router.add_get("/cameras/{slug}/last-liveview", last_liveview_info_handler)
    app.router.add_get(
        "/cameras/{slug}/last-liveview.ts", last_liveview_download_handler
    )
    app.router.add_get(
        "/cameras/{slug}/last-liveview.mp4", last_liveview_mp4_download_handler
    )
    app.router.add_get("/cameras/{slug}/hls/index.m3u8", hls_playlist_handler)
    app.router.add_get("/cameras/{slug}/hls/{filename}", hls_segment_handler)

    async def cleanup_context(_app: web.Application):
        cleanup_task = asyncio.create_task(hls_manager.cleanup_loop())
        await auth_controller.start_startup_login()
        try:
            yield
        finally:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
            await hls_manager.stop_all()
            await auth_controller.close()

    app.cleanup_ctx.append(cleanup_context)
    return app
