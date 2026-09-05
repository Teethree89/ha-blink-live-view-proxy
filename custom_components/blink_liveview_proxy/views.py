"""HTTP views for the Blink live-view proxy integration."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

from aiohttp import ClientError, ClientResponse, ClientTimeout, WSMsgType, web

from homeassistant.components.http import HomeAssistantView, require_admin
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers.http import KEY_AUTHENTICATED

from . import prerequisites
from .api import BlinkLiveviewProxyClient, ProxyAuthError, ProxyConnectionError
from .failures import failure_payload
from .dashboard_yaml import render_dashboard_yaml
from .lovelace import resource_collection, resource_mode
from .playlist import tokenise_playlist
from .const import (
    ASSET_URL_BASE,
    CONF_BASE_URL,
    DEFAULT_STREAM_SECONDS,
    DOMAIN,
    ENVIRONMENT_PROXY_VERSION,
    FRONTEND_RESOURCE_URL,
    LEGACY_ASSET_URL_BASE,
    LEGACY_FRONTEND_RESOURCE_URL,
    MINIMUM_HA_VERSION,
    REPOSITORY_URL,
    REQUIRED_BLINKPY_VERSION,
)
from .updates import UpdateAborted, async_start_update
from .version_check import can_start_update, infer_version, is_behind, update_blocker

LOGGER = logging.getLogger(__name__)

STATIC_ROOT = Path(__file__).parent / "frontend"
# The wordmarks and icon live in brand/, beside manifest.json, where Home
# Assistant 2026.3.0 and newer serve them itself at /api/brands. This project
# still supports 2024.11.0, where that route does not exist, so the panel gets
# them from here instead and looks the same on every supported core.
BRAND_ROOT = Path(__file__).parent / "brand"
PLAYER_LIBRARY_URL = f"{ASSET_URL_BASE}/mpegts.min.js"
BROWSER_TOKEN_TTL_SECONDS = 10 * 60
BROWSER_TOKEN_MAX_COUNT = 128


def async_register_views(hass: HomeAssistant) -> None:
    """Register browser-facing proxy views."""
    if hass.data.setdefault(DOMAIN, {}).get("_views_registered"):
        return

    hass.http.register_view(BlinkLiveviewProxyAssetView(hass))
    hass.http.register_view(BlinkLiveviewProxyLegacyAssetView(hass))
    hass.http.register_view(BlinkLiveviewProxyPlayerView(hass))
    hass.http.register_view(BlinkLiveviewProxyBrowserTokenView(hass))
    hass.http.register_view(BlinkLiveviewProxyMpegtsView(hass))
    # Order matters: index.m3u8 must be registered before the {filename}
    # catch-all or the playlist would be served as a segment.
    hass.http.register_view(BlinkLiveviewProxyHlsPlaylistView(hass))
    hass.http.register_view(BlinkLiveviewProxyHlsSegmentView(hass))
    hass.http.register_view(BlinkLiveviewProxyPttView(hass))
    hass.http.register_view(BlinkLiveviewProxyStopView(hass))
    hass.http.register_view(BlinkLiveviewProxyLastLiveviewInfoView(hass))
    hass.http.register_view(BlinkLiveviewProxyLastLiveviewDownloadView(hass))
    hass.http.register_view(BlinkLiveviewProxyLastLiveviewMp4DownloadView(hass))
    hass.http.register_view(BlinkLiveviewProxySnapshotRefreshView(hass))
    hass.http.register_view(BlinkLiveviewProxyClipsView(hass))
    hass.http.register_view(BlinkLiveviewProxyClipDownloadView(hass))
    hass.http.register_view(BlinkLiveviewProxyClipThumbnailView(hass))
    hass.http.register_view(BlinkLiveviewProxyClipsViewerView(hass))
    hass.http.register_view(BlinkLiveviewProxyAuthStatusView(hass))
    hass.http.register_view(BlinkLiveviewProxyAuthActionView(hass))
    hass.http.register_view(BlinkLiveviewProxyPanelView(hass))
    hass.http.register_view(BlinkLiveviewProxyPanelUpdateView(hass))
    hass.http.register_view(BlinkLiveviewProxyPanelYamlView(hass))
    hass.data[DOMAIN]["_views_registered"] = True


class BlinkLiveviewProxyAssetView(HomeAssistantView):
    """Serve package frontend assets used by dashboards and the player.

    Not "/static/": Home Assistant's service worker CacheFirsts any same-origin
    URL containing that segment, which pinned these files to whatever the
    browser saw first. See ASSET_URL_BASE in const.py.
    """

    requires_auth = False
    url = f"{ASSET_URL_BASE}/{{filename}}"
    name = "api:blink_liveview_proxy:assets"

    _content_types: ClassVar[dict[str, str]] = {
        "blink-liveview-dialog.js": "application/javascript",
        "blink-proxy-auth-panel.js": "application/javascript",
        "blink-liveview-icons.js": "application/javascript",
        "mpegts.min.js": "application/javascript",
        "logo.png": "image/png",
        "dark_logo.png": "image/png",
        "icon.png": "image/png",
    }
    # An allow-list keyed by name, so no filename from the URL ever reaches a
    # path join and the two source folders stay an implementation detail.
    _roots: ClassVar[dict[str, Path]] = {
        "logo.png": BRAND_ROOT,
        "dark_logo.png": BRAND_ROOT,
        "icon.png": BRAND_ROOT,
    }

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, _request: web.Request, filename: str) -> web.FileResponse:
        """Return one bundled frontend asset."""
        if filename not in self._content_types:
            raise web.HTTPNotFound()

        path = self._roots.get(filename, STATIC_ROOT) / filename
        if not path.exists():
            raise web.HTTPNotFound(text=f"Missing static asset: {filename}\n")

        return web.FileResponse(
            path,
            headers={
                "Cache-Control": "no-cache",
                "Content-Type": self._content_types[filename],
            },
        )


class BlinkLiveviewProxyLegacyAssetView(BlinkLiveviewProxyAssetView):
    """The pre-0.6.2 asset path, still answered.

    Every dashboard, YAML resource list and hand-written config in the wild
    points here, and this project's worst failure mode is a frontend module
    that does not load. Serving both costs one route; breaking this one costs
    people a silently dead dashboard they cannot debug.

    Files fetched through this path stay subject to the service worker's
    CacheFirst rule. That is the reason to move, not a reason to 404.
    """

    url = f"{LEGACY_ASSET_URL_BASE}/{{filename}}"
    name = "api:blink_liveview_proxy:static"


def _runtime(hass: HomeAssistant) -> dict[str, Any]:
    """Return the first configured integration runtime."""
    for key, value in hass.data.get(DOMAIN, {}).items():
        if not str(key).startswith("_") and isinstance(value, dict):
            return value
    raise web.HTTPServiceUnavailable(text="Blink live-view proxy is not configured\n")


def _runtime_entry(hass: HomeAssistant) -> tuple[str, dict[str, Any]]:
    """Return the first configured entry id and runtime."""
    for key, value in hass.data.get(DOMAIN, {}).items():
        if not str(key).startswith("_") and isinstance(value, dict):
            return str(key), value
    raise web.HTTPServiceUnavailable(text="Blink live-view proxy is not configured\n")


def _client(hass: HomeAssistant) -> BlinkLiveviewProxyClient:
    return _runtime(hass)["client"]


def _auth_client(hass: HomeAssistant) -> BlinkLiveviewProxyClient:
    """Return a proxy client even while camera discovery is awaiting reauth."""
    try:
        return _client(hass)
    except web.HTTPServiceUnavailable:
        clients = hass.data.get(DOMAIN, {}).get("_auth_clients", {})
        if clients:
            return next(iter(clients.values()))
        raise


def _auth_failure_payload(err: Exception) -> dict[str, Any]:
    """Describe a failure by what the proxy answered, never by its error text.

    A reachable, correctly configured proxy can still refuse these routes — it
    may predate them, or be running without a token — and telling that user to
    check their URL and token sends them to look at the one thing that is fine.
    """
    if isinstance(err, ProxyAuthError):
        status: int | None = 401
    elif isinstance(err, ProxyConnectionError):
        status = err.status
    else:
        status = None
    LOGGER.warning(
        "Proxy authentication request failed (%s, upstream status %s)",
        type(err).__name__,
        status,
    )
    return failure_payload(status)


def _stream_seconds(hass: HomeAssistant) -> int:
    try:
        value = int(_runtime(hass).get("stream_seconds", DEFAULT_STREAM_SECONDS))
    except (TypeError, ValueError):
        value = DEFAULT_STREAM_SECONDS
    return max(10, min(300, value))


def _camera(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    coordinator = _runtime(hass)["coordinator"]
    for camera in coordinator.data.get("cameras", []):
        if camera.get("slug") == slug:
            return camera
    raise web.HTTPNotFound(text=f"Unknown camera slug: {slug}\n")


def _camera_inventory(hass: HomeAssistant) -> list[dict[str, str]]:
    """Return every proxy camera as slug and display name, for the clip viewer's select."""
    cameras = _runtime(hass)["coordinator"].data.get("cameras", [])
    return [
        {"slug": str(item["slug"]), "name": str(item.get("name") or item["slug"])}
        for item in cameras
        if item.get("slug")
    ]


def _live_camera_state(hass: HomeAssistant, slug: str):
    """Return the HA camera state for a proxy slug."""
    for state in hass.states.async_all("camera"):
        if state.attributes.get("proxy_slug") == slug:
            return state
    raise web.HTTPNotFound(text=f"Unknown live camera slug: {slug}\n")


def _browser_tokens(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Return short-lived player tokens accepted by browser media requests."""
    return hass.data.setdefault(DOMAIN, {}).setdefault("_browser_tokens", {})


def _prune_browser_tokens(hass: HomeAssistant) -> None:
    """Remove expired player tokens and cap the in-memory token store."""
    store = _browser_tokens(hass)
    now = time.monotonic()
    for token, details in list(store.items()):
        if float(details.get("expires_at", 0)) <= now:
            store.pop(token, None)

    overflow = len(store) - BROWSER_TOKEN_MAX_COUNT
    if overflow > 0:
        oldest = sorted(
            store.items(),
            key=lambda item: float(item[1].get("expires_at", 0)),
        )
        for token, _details in oldest[:overflow]:
            store.pop(token, None)


def _issue_browser_token(hass: HomeAssistant, slug: str) -> str:
    """Issue a short-lived token for one browser live-view modal."""
    _prune_browser_tokens(hass)
    token = secrets.token_urlsafe(32)
    _browser_tokens(hass)[token] = {
        "slug": slug,
        "expires_at": time.monotonic() + BROWSER_TOKEN_TTL_SECONDS,
    }
    return token


def _is_browser_token_valid(hass: HomeAssistant, provided: str, slug: str) -> bool:
    """Return whether a browser token is valid for the requested camera."""
    if not provided:
        return False

    _prune_browser_tokens(hass)
    details = _browser_tokens(hass).get(provided)
    if not details or details.get("slug") != slug:
        return False

    details["expires_at"] = time.monotonic() + BROWSER_TOKEN_TTL_SECONDS
    return True


def _authorize_browser_request(
    hass: HomeAssistant,
    request: web.Request,
    slug: str,
    *,
    issue_browser_token: bool = False,
) -> str:
    """Authorize browser navigation with HA auth or a camera access token."""
    provided = request.query.get("token", "")
    if _is_browser_token_valid(hass, provided, slug):
        return provided

    state = _live_camera_state(hass, slug)
    camera_token = str(state.attributes.get("access_token") or "")
    if request.get(KEY_AUTHENTICATED, False):
        if issue_browser_token:
            return _issue_browser_token(hass, slug)
        return camera_token or provided

    if provided and camera_token and secrets.compare_digest(provided, camera_token):
        if issue_browser_token:
            return _issue_browser_token(hass, slug)
        return provided

    raise web.HTTPForbidden(text="Missing or invalid camera token\n")


def _snapshot_style(hass: HomeAssistant, camera: dict[str, Any]) -> str:
    """Return a CSS background image backed by the normal Blink snapshot."""
    source_entity_id = str(camera.get("entity_id") or "")
    if not source_entity_id:
        return ""

    snapshot_url = _snapshot_url(hass, source_entity_id)
    if not snapshot_url:
        return ""
    return (
        f"background-image:linear-gradient(rgba(2,6,23,.66),rgba(2,6,23,.74)),"
        f"url('{snapshot_url}');"
    )


def _snapshot_url(
    hass: HomeAssistant, source_entity_id: str, cache: str | None = None
) -> str:
    """Return an authenticated Home Assistant camera proxy URL."""
    source_state = hass.states.get(source_entity_id)
    source_token = ""
    if source_state is not None:
        source_token = str(source_state.attributes.get("access_token") or "")
    query: dict[str, str] = {}
    if source_token:
        query["token"] = source_token
    if cache:
        query["cache"] = cache
    query_string = f"?{urlencode(query)}" if query else ""
    return f"/api/camera_proxy/{quote(source_entity_id, safe='')}{query_string}"


async def _open_proxy_response(
    client: BlinkLiveviewProxyClient,
    path: str,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> ClientResponse:
    """Open a streaming response from the local proxy."""
    try:
        response = await client._session.get(  # noqa: SLF001
            client.proxy_url(path, query),
            headers={**client.auth_headers(), **(headers or {})},
            timeout=ClientTimeout(connect=15, sock_connect=15, sock_read=75, total=None),
        )
    except ClientError as err:
        raise web.HTTPBadGateway(text=f"Proxy request failed: {err}\n") from err

    if response.status in (401, 403):
        response.close()
        raise web.HTTPUnauthorized(text="Proxy token rejected\n")
    if response.status == 404:
        response.close()
        raise web.HTTPNotFound(text="Proxy resource not found\n")
    if response.status == 503:
        # The proxy is up but its Blink client is not ready - it is signing in,
        # or reconnecting. Collapsing that into 502 told the browser the
        # gateway was broken and left a permanent broken thumbnail; 503 is
        # honest and is what the viewer retries on.
        body = await response.text()
        response.close()
        raise web.HTTPServiceUnavailable(
            text=body or "The proxy is not ready yet\n"
        )
    if response.status == 416:
        # An unsatisfiable Range. The proxy answers it correctly, and says in
        # Content-Range how long the resource actually is; collapsing that into
        # 502 told the browser the gateway was broken and threw away the one
        # header that would have let it ask again for something valid.
        content_range = response.headers.get("Content-Range")
        body = await response.text()
        response.close()
        raise web.HTTPRequestRangeNotSatisfiable(
            text=body or "Requested range not satisfiable\n",
            headers={"Content-Range": content_range} if content_range else None,
        )
    if response.status == 429:
        retry_after = response.headers.get("Retry-After", "30")
        body = await response.text()
        response.close()
        raise web.HTTPTooManyRequests(
            text=body or "Blink live view cooldown is active\n",
            headers={"Retry-After": retry_after},
        )
    if response.status >= 400:
        body = await response.text()
        response.close()
        raise web.HTTPBadGateway(
            text=body or f"Proxy returned HTTP {response.status}\n"
        )
    return response


async def _proxy_stream(
    hass: HomeAssistant,
    request: web.Request,
    path: str,
    content_type: str,
    query: dict[str, str] | None = None,
    *,
    download_filename: str | None = None,
    cache_control: str = "no-store",
) -> web.StreamResponse:
    """Stream bytes from the local proxy to the browser.

    A Range header is forwarded and a 206 comes back as a 206. The proxy serves
    cached clips as files, which answer ranges, and that is what lets the clip
    player seek - and what Safari requires before it will play an MP4 at all.
    """
    forward: dict[str, str] = {}
    if range_header := request.headers.get("Range"):
        forward["Range"] = range_header
    upstream = await _open_proxy_response(_client(hass), path, query, forward or None)
    headers = {
        "Cache-Control": cache_control,
        "X-Accel-Buffering": "no",
    }
    for name in ("Content-Range", "Content-Length", "Accept-Ranges"):
        if name in upstream.headers:
            headers[name] = upstream.headers[name]
    if download_filename:
        headers["Content-Disposition"] = (
            f'attachment; filename="{download_filename}"'
        )
    else:
        upstream_disposition = upstream.headers.get("Content-Disposition")
        if upstream_disposition:
            headers["Content-Disposition"] = upstream_disposition
    response = web.StreamResponse(
        status=upstream.status if upstream.status in (200, 206) else 200,
        headers=headers,
    )
    response.content_type = content_type

    try:
        await response.prepare(request)
        async for chunk in upstream.content.iter_chunked(102400):
            if not hass.is_running:
                break
            await response.write(chunk)
    except (ConnectionResetError, TimeoutError, ClientError):
        LOGGER.debug("Browser stream closed for %s", path)
    finally:
        upstream.close()
    return response


def _player_html(
    hass: HomeAssistant,
    slug: str,
    camera: dict[str, Any],
    access_token: str,
) -> str:
    """Return the direct live-view player page."""
    safe_slug = quote(slug, safe="")
    name = html.escape(str(camera.get("name") or slug.replace("_", " ").title()))
    snapshot_style = _snapshot_style(hass, camera)
    token_json = json.dumps(access_token)
    stream_seconds = _stream_seconds(hass)
    ptt_supported = json.dumps(bool(camera.get("ptt_supported", True)))

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Blink Live {name}</title>
<link rel="icon" href="{ASSET_URL_BASE}/icon.png">
<style>
html,body {{
  margin:0;
  width:100%;
  height:100%;
  background:#05070a;
  color:#f8fafc;
  font-family:Arial,Helvetica,sans-serif;
}}
body {{
  overflow:hidden;
}}
.stage {{
  position:fixed;
  inset:0;
  display:grid;
  place-items:center;
  background:#05070a center/cover no-repeat;
  {snapshot_style}
}}
video {{
  position:absolute;
  inset:0;
  width:100%;
  height:100%;
  object-fit:contain;
  background:#05070a;
  opacity:0;
  transition:opacity .18s ease;
}}
video.ready {{
  opacity:1;
}}
.overlay {{
  position:absolute;
  inset:0;
  display:grid;
  place-items:center;
  text-align:center;
  background:linear-gradient(rgba(2,6,23,.25),rgba(2,6,23,.5));
  transition:opacity .18s ease;
}}
.overlay.hidden {{
  opacity:0;
  pointer-events:none;
}}
.panel {{
  display:grid;
  gap:14px;
  justify-items:center;
  max-width:min(520px,calc(100vw - 32px));
  padding-bottom:env(safe-area-inset-bottom, 0px);
}}
.spinner {{
  width:58px;
  height:58px;
  border:7px solid rgba(226,232,240,.24);
  border-top-color:#7dd3fc;
  border-radius:999px;
  animation:spin 1s linear infinite;
}}
@keyframes spin {{ to {{ transform:rotate(360deg); }} }}
.title {{
  font-size:clamp(22px,4vw,38px);
  font-weight:700;
  -webkit-user-select:none;
  user-select:none;
}}
.status {{
  color:#cbd5e1;
  font-size:16px;
  line-height:1.35;
}}
.actions {{
  display:flex;
  flex-wrap:wrap;
  gap:10px;
  justify-content:center;
}}
.actions[hidden] {{
  display:none;
}}
.live-actions {{
  position:absolute;
  top:calc(16px + env(safe-area-inset-top, 0px));
  right:calc(16px + env(safe-area-inset-right, 0px));
  z-index:4;
  display:flex;
  gap:10px;
}}
.live-actions[hidden] {{
  display:none;
}}
.live-actions.bottom-gutter {{
  top:auto;
  bottom:calc(16px + env(safe-area-inset-bottom, 0px));
}}
button,a.button {{
  appearance:none;
  border:0;
  /* Hold Talk is a press-and-hold control, and a long press on a phone
     selects the label and raises the callout menu unless both are refused. */
  -webkit-user-select:none;
  user-select:none;
  -webkit-touch-callout:none;
  -webkit-tap-highlight-color:transparent;
  border-radius:6px;
  background:#0284c7;
  color:#f8fafc;
  font-size:15px;
  font-weight:700;
  padding:10px 14px;
  text-decoration:none;
  cursor:pointer;
}}
button:disabled {{
  cursor:wait;
  opacity:.7;
}}
a.button.secondary,button.secondary {{
  background:rgba(148,163,184,.22);
}}
button.danger {{
  background:#dc2626;
}}
button.talk {{
  min-width:94px;
  background:#0f766e;
}}
button.talk.pending {{
  background:#a16207;
}}
button.talk.active {{
  background:#16a34a;
}}
/* On a phone the stage is far bigger than a 16:9 picture in one direction or
   the other - much taller in portrait, much wider in landscape - and a video
   element that fills it puts iOS's native control bar, AirPlay and all, at the
   bottom of the black rather than under the picture. Letting the element
   shrink-wrap the picture keeps those controls where they belong.

   It has to be flex, not the grid above. A grid row with no explicit size is
   sized by its content, so `width:100%; height:auto` made the row as tall as
   the video wanted to be and `max-height:100%` then measured itself against
   that same grown row - clamping nothing, and cropping the bottom off a
   landscape live view. A flex container has a definite height here, so the
   percentages resolve against the screen and the picture is letterboxed
   instead of overflowing. width and height stay auto so the element takes the
   video's own shape, and the two max- rules bound it on both axes. */
@media (max-width: 720px), (max-height: 520px) {{
  /* Centred, not tucked into the corner. On a phone the picture reaches the
     edges and these sat on top of the native mute and AirPlay controls. */
  .live-actions {{
    left:50%;
    right:auto;
    transform:translateX(-50%);
  }}
  .stage {{
    display:flex;
    align-items:center;
    justify-content:center;
  }}
  video {{
    position:static;
    width:auto;
    height:auto;
    max-width:100%;
    max-height:100%;
  }}
}}
</style>
</head>
<body>
<main class="stage">
  <video id="video" muted playsinline autoplay controls></video>
  <section id="overlay" class="overlay">
    <div class="panel">
      <div id="spinner" class="spinner"></div>
      <div class="title">Blink Live {name}</div>
      <div id="status" class="status">Starting live view</div>
      <div id="actions" class="actions" hidden>
        <button id="restart" type="button">Start Again</button>
        <button id="save" class="secondary" type="button">Save MP4</button>
      </div>
    </div>
  </section>
  <div id="liveActions" class="live-actions" hidden>
    <button id="sound" class="secondary" type="button" aria-pressed="false">Unmute</button>
    <button id="talk" class="talk" type="button" disabled>Hold Talk</button>
    <button id="end" class="danger" type="button">End</button>
  </div>
</main>
<script src="{PLAYER_LIBRARY_URL}"></script>
<script>
if (window.mpegts && mpegts.LoggingControl) {{
  mpegts.LoggingControl.applyConfig({{
    enableAll: false,
    enableVerbose: false,
    enableDebug: false,
    enableInfo: false,
    enableWarn: true,
    enableError: true
  }});
}}
const slug = "{safe_slug}";
const seconds = {stream_seconds};
const accessToken = {token_json};
const pttSupported = {ptt_supported};
const sessionId = window.crypto && crypto.randomUUID
  ? crypto.randomUUID()
  : `${{Date.now()}}-${{Math.random().toString(36).slice(2)}}`;
const video = document.getElementById("video");
const overlay = document.getElementById("overlay");
const spinner = document.getElementById("spinner");
const statusText = document.getElementById("status");
const actions = document.getElementById("actions");
const liveActions = document.getElementById("liveActions");
const restart = document.getElementById("restart");
const save = document.getElementById("save");
const talk = document.getElementById("talk");
const sound = document.getElementById("sound");
const endButton = document.getElementById("end");
let player = null;
let endTimer = null;
let talkWs = null;
let talkStream = null;
let talkContext = null;
let talkSource = null;
let talkProcessor = null;
let talkMute = null;
let talkActive = false;
let talkStarting = false;
let talkListening = false;

// Sound, and why the stream starts without it.
//
// Every browser refuses to autoplay audio until someone has interacted with
// the page, so the only live view that starts on its own is a muted one - and
// a live view that silently never had sound reads as a broken one, especially
// next to a saved clip, which plays with audio. The native control bar can
// unmute, once you know to tap the picture to find it. This is the same
// switch, in the open, next to the controls that are already there.
const SOUND_PREFERENCE = "blink-liveview-sound";
let restoringMute = false;

function soundWanted() {{
  try {{
    return window.localStorage.getItem(SOUND_PREFERENCE) === "on";
  }} catch (err) {{
    // Storage can be unavailable outright in a private window or a webview
    // with site data blocked. Sound then simply does not persist.
    return false;
  }}
}}

function rememberSound(on) {{
  try {{
    window.localStorage.setItem(SOUND_PREFERENCE, on ? "on" : "off");
  }} catch (err) {{}}
}}

function syncSoundButton() {{
  sound.textContent = video.muted ? "Unmute" : "Mute";
  sound.setAttribute("aria-pressed", video.muted ? "false" : "true");
}}

function toggleSound() {{
  video.muted = !video.muted;
  if (!video.muted && video.volume === 0) video.volume = 1;
  if (video.paused) video.play().catch(() => {{}});
}}

async function startPlayback() {{
  // A tap on this frame counts as the gesture that permits sound, so someone
  // who unmuted last time gets sound this time without asking again. If the
  // browser refuses anyway, fall back to the muted start it does allow rather
  // than leaving a still picture and "Tap play".
  if (soundWanted()) video.muted = false;
  try {{
    await video.play();
    return;
  }} catch (err) {{
    if (video.muted) {{
      statusText.textContent = "Tap play to start live view";
      return;
    }}
  }}
  restoringMute = true;
  video.muted = true;
  restoringMute = false;
  syncSoundButton();
  try {{
    await video.play();
  }} catch (err) {{
    statusText.textContent = "Tap play to start live view";
  }}
}}

function positionLiveActions() {{
  liveActions.classList.remove("bottom-gutter");
  if (window.innerWidth >= window.innerHeight || liveActions.hidden) return;

  const videoRect = video.getBoundingClientRect();
  const roomBelow = window.innerHeight - videoRect.bottom;
  // Safari does not expose whether its native media controls are currently
  // showing. The rendered geometry tells us what matters: if a portrait
  // letterbox leaves enough room below the video for both these buttons and
  // the home indicator, use that gutter. Otherwise keep them at the top,
  // clear of the native controls along the video's bottom edge.
  if (roomBelow >= liveActions.offsetHeight + 80) {{
    liveActions.classList.add("bottom-gutter");
  }}
}}

function positionLiveActionsThroughRotation() {{
  positionLiveActions();
  for (const delay of [60, 180, 400, 800]) setTimeout(positionLiveActions, delay);
}}

function streamUrl() {{
  const token = encodeURIComponent(accessToken || "");
  const session = encodeURIComponent(sessionId);
  const path = `/api/blink_liveview_proxy/cameras/${{slug}}/mpegts?token=${{token}}&seconds=${{seconds}}&force=1&session=${{session}}&cache=${{Date.now()}}`;
  return new URL(path, window.location.origin).href;
}}

function hlsUrl() {{
  const token = encodeURIComponent(accessToken || "");
  const session = encodeURIComponent(sessionId);
  const path = `/api/blink_liveview_proxy/cameras/${{slug}}/hls/index.m3u8?token=${{token}}&seconds=${{seconds}}&force=1&session=${{session}}&cache=${{Date.now()}}`;
  return new URL(path, window.location.origin).href;
}}

function pttUrl() {{
  const token = encodeURIComponent(accessToken || "");
  const session = encodeURIComponent(sessionId);
  const path = `/api/blink_liveview_proxy/cameras/${{slug}}/ptt?token=${{token}}&session=${{session}}`;
  const url = new URL(path, window.location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}}

function downloadUrl() {{
  const token = encodeURIComponent(accessToken || "");
  const path = `/api/blink_liveview_proxy/cameras/${{slug}}/last-liveview.mp4?token=${{token}}&cache=${{Date.now()}}`;
  return new URL(path, window.location.origin).href;
}}

function stopUrl() {{
  const token = encodeURIComponent(accessToken || "");
  const path = `/api/blink_liveview_proxy/cameras/${{slug}}/stop?token=${{token}}`;
  return new URL(path, window.location.origin).href;
}}

// Tell the proxy the live view is over. On the MPEG-TS path that happens by
// itself when the connection drops; the HLS path has only an idle timeout,
// which on a tuned install can be the better part of a minute - long enough
// that the camera keeps streaming to nobody, and that the cached copy is not
// finalized until well after anything waiting for it has given up.
async function stopUpstream() {{
  try {{
    await fetch(stopUrl(), {{
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      keepalive: true,
    }});
  }} catch (err) {{}}
}}

function delay(ms) {{
  return new Promise((resolve) => setTimeout(resolve, ms));
}}

function downloadFilename(response) {{
  const fallback = `${{slug}}_last_liveview.mp4`;
  const header = response.headers.get("content-disposition") || "";
  const match = header.match(/filename="?([^";]+)"?/i);
  return match ? match[1] : fallback;
}}

async function fetchLastViewMp4(retries = 2) {{
  let lastError = null;
  for (let attempt = 0; attempt <= retries; attempt += 1) {{
    const response = await fetch(downloadUrl(), {{
      cache: "no-store",
      credentials: "same-origin"
    }});
    if (response.ok) {{
      return response;
    }}
    lastError = new Error(`HTTP ${{response.status}}`);
    if (attempt < retries) {{
      await delay(700);
    }}
  }}
  throw lastError || new Error("Could not download MP4");
}}

async function downloadMp4(response) {{
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = downloadFilename(response);
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
}}

function pcm16Buffer(floatData) {{
  const pcm = new Int16Array(floatData.length);
  for (let index = 0; index < floatData.length; index += 1) {{
    const sample = Math.max(-1, Math.min(1, floatData[index]));
    pcm[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }}
  return pcm.buffer;
}}

function setTalkButton(state, label) {{
  talk.classList.toggle("pending", state === "pending");
  talk.classList.toggle("active", state === "listening");
  talk.textContent = label;
}}

function handleTalkStatus(data) {{
  if (!data || typeof data !== "object") {{
    return;
  }}
  if (data.type === "started") {{
    if (talkActive) {{
      setTalkButton("pending", "Warming Up");
    }}
  }} else if (data.type === "listening") {{
    if (talkActive) {{
      talkListening = true;
      setTalkButton("listening", "Listening");
    }}
  }} else if (data.type === "stopped") {{
    talkListening = false;
    if (!talkActive) {{
      setTalkButton("idle", "Hold Talk");
    }}
  }} else if (data.type === "error" && data.message) {{
    statusText.textContent = data.message;
    talkListening = false;
    setTalkButton("idle", "Hold Talk");
  }}
}}

function connectTalkSocket() {{
  return new Promise((resolve, reject) => {{
    const socket = new WebSocket(pttUrl());
    socket.binaryType = "arraybuffer";
    const timeout = setTimeout(() => {{
      socket.close();
      reject(new Error("Push-to-talk connection timed out"));
    }}, 5000);
    socket.addEventListener("open", () => {{
      clearTimeout(timeout);
      resolve(socket);
    }}, {{ once: true }});
    socket.addEventListener("error", () => {{
      clearTimeout(timeout);
      reject(new Error("Push-to-talk connection failed"));
    }}, {{ once: true }});
    socket.addEventListener("message", (event) => {{
      try {{
        handleTalkStatus(JSON.parse(event.data));
      }} catch (err) {{}}
    }});
  }});
}}

async function startTalk(event) {{
  if (event) {{
    event.preventDefault();
  }}
  if (!pttSupported || talkActive || talkStarting || !video.classList.contains("ready")) {{
    return;
  }}
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!window.isSecureContext) {{
    statusText.textContent = "Microphone needs HTTPS or a trusted local browser origin.";
    return;
  }}
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !AudioContextClass) {{
    statusText.textContent = "Microphone is not available in this browser.";
    return;
  }}

  talkStarting = true;
  talkActive = true;
  talkListening = false;
  setTalkButton("pending", "Connecting");

  try {{
    talkStream = await navigator.mediaDevices.getUserMedia({{
      audio: {{
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true
      }},
      video: false
    }});
    talkContext = new AudioContextClass();
    await talkContext.resume();
    talkWs = await connectTalkSocket();
    talkWs.send(JSON.stringify({{
      type: "start",
      sampleRate: Math.round(talkContext.sampleRate)
    }}));

    talkSource = talkContext.createMediaStreamSource(talkStream);
    talkProcessor = talkContext.createScriptProcessor(2048, 1, 1);
    talkMute = talkContext.createGain();
    talkMute.gain.value = 0;
    talkProcessor.onaudioprocess = (audioEvent) => {{
      if (!talkWs || talkWs.readyState !== WebSocket.OPEN || !talkActive) {{
        return;
      }}
      talkWs.send(pcm16Buffer(audioEvent.inputBuffer.getChannelData(0)));
    }};
    talkSource.connect(talkProcessor);
    talkProcessor.connect(talkMute);
    talkMute.connect(talkContext.destination);
    talkStarting = false;
  }} catch (err) {{
    talkStarting = false;
    statusText.textContent = "Could not start microphone.";
    await stopTalk();
  }}
}}

async function stopTalk(event) {{
  if (event) {{
    event.preventDefault();
  }}
  const wasActive = talkActive;
  talkStarting = false;
  talkActive = false;
  talkListening = false;
  setTalkButton("idle", "Hold Talk");

  if (talkProcessor) {{
    talkProcessor.onaudioprocess = null;
    try {{ talkProcessor.disconnect(); }} catch (err) {{}}
    talkProcessor = null;
  }}
  if (talkSource) {{
    try {{ talkSource.disconnect(); }} catch (err) {{}}
    talkSource = null;
  }}
  if (talkMute) {{
    try {{ talkMute.disconnect(); }} catch (err) {{}}
    talkMute = null;
  }}
  if (talkStream) {{
    for (const track of talkStream.getTracks()) {{
      track.stop();
    }}
    talkStream = null;
  }}
  if (talkWs) {{
    if (talkWs.readyState === WebSocket.OPEN && wasActive) {{
      talkWs.send(JSON.stringify({{ type: "stop" }}));
    }}
    talkWs.close();
    talkWs = null;
  }}
  if (talkContext) {{
    try {{ await talkContext.close(); }} catch (err) {{}}
    talkContext = null;
  }}
}}

async function saveLastView() {{
  const originalText = save.textContent;
  save.disabled = true;
  save.textContent = "Saving MP4";

  try {{
    // Belt and braces: if anything still has the session open, close it so
    // the file being fetched is the one just watched rather than an older
    // one. A no-op when it has already stopped.
    await stopUpstream();
    const response = await fetchLastViewMp4(3);
    await downloadMp4(response);
    save.textContent = "Saved";
    setTimeout(() => {{
      save.textContent = originalText;
    }}, 1400);
  }} catch (err) {{
    save.textContent = originalText;
    setEnded("Could not save the last live view.");
  }} finally {{
    save.disabled = false;
  }}
}}

// One job, not three. This used to end the stream, poll for the recording to
// be finalized and download it, and the polling could not work on the HLS
// path: the session is only finalized when it stops, and it did not stop
// until the idle timeout - three quarters of a minute on a tuned install,
// long after the poll had given up and reported a failure for a recording
// that did in fact arrive. Ending and saving are two buttons now, and the
// save one appears once the stream has actually ended.
async function endCurrentStream() {{
  endButton.disabled = true;
  try {{
    await endSession("Live view ended. Save it, or start again.");
  }} finally {{
    endButton.disabled = false;
  }}
}}

// Every way a live view can finish goes through here, so the upstream session
// is always closed and its recording always finalized before Save MP4 is
// offered - whether the viewer ended it, the timer elapsed, or the camera did.
async function endSession(message) {{
  stopPlayer();
  setLoading("Ending live view");
  await stopUpstream();
  setEnded(message);
}}

function setLoading(message) {{
  overlay.classList.remove("hidden");
  video.classList.remove("ready");
  spinner.hidden = false;
  actions.hidden = true;
  liveActions.hidden = true;
  talk.disabled = true;
  statusText.textContent = message;
}}

function setEnded(message) {{
  overlay.classList.remove("hidden");
  spinner.hidden = true;
  actions.hidden = false;
  liveActions.hidden = true;
  talk.disabled = true;
  statusText.textContent = message;
}}

function stopPlayer() {{
  stopTalk();
  if (endTimer) {{
    clearTimeout(endTimer);
    endTimer = null;
  }}
  video.classList.remove("ready");
  liveActions.hidden = true;
  talk.disabled = true;
  video.onplaying = null;
  video.onended = null;
  if (player) {{
    try {{ player.pause(); }} catch (err) {{}}
    try {{ player.unload(); }} catch (err) {{}}
    try {{ player.detachMediaElement(); }} catch (err) {{}}
    try {{ player.destroy(); }} catch (err) {{}}
    player = null;
  }}
  video.removeAttribute("src");
  video.load();
}}

async function startPlayer() {{
  stopPlayer();
  setLoading("Waking camera and waiting for video");

  const canMse = !!(window.mpegts && mpegts.getFeatureList().mseLivePlayback);
  const canNativeHls = video.canPlayType("application/vnd.apple.mpegurl") !== "";

  if (!canMse && canNativeHls) {{
    // iOS Safari and the Home Assistant companion app's WKWebView have no
    // Media Source Extensions, so mpegts.js can never run there and the direct
    // MPEG-TS player always dies with E-001b. They do play HLS natively, and
    // the proxy already produces an HLS rendition, so use it.
    video.onplaying = () => {{
      video.classList.add("ready");
      overlay.classList.add("hidden");
      actions.hidden = true;
      liveActions.hidden = false;
      talk.hidden = !pttSupported;
      talk.disabled = !pttSupported;
      positionLiveActions();
    }};
    video.onended = () => {{
      endSession("Live view ended.");
    }};
    video.onerror = () => {{
      endSession("Live view ended or the camera stopped sending video.");
    }};
    video.src = hlsUrl();
    video.load();
    await startPlayback();
    endTimer = setTimeout(() => {{
      endSession(`${{seconds}} second live view finished.`);
    }}, (seconds + 5) * 1000);
    return;
  }}

  if (!canMse) {{
    setEnded("This browser cannot play the direct MPEG-TS stream. E-001b");
    return;
  }}

  player = mpegts.createPlayer({{
    type: "mpegts",
    isLive: true,
    url: streamUrl()
  }}, {{
    enableWorker: false,
    enableStashBuffer: false,
    autoCleanupSourceBuffer: true,
    autoCleanupMaxBackwardDuration: 8,
    autoCleanupMinBackwardDuration: 3,
    liveBufferLatencyChasing: true,
    liveBufferLatencyMaxLatency: 3,
    liveBufferLatencyMinRemain: 1,
    stashInitialSize: 96 * 1024
  }});

  player.on(mpegts.Events.ERROR, () => {{
    endSession("Live view ended or the camera stopped sending video.");
  }});

  video.onplaying = () => {{
    video.classList.add("ready");
    overlay.classList.add("hidden");
    actions.hidden = true;
    liveActions.hidden = false;
    talk.hidden = !pttSupported;
    talk.disabled = !pttSupported;
    positionLiveActions();
  }};

  video.onended = () => {{
    endSession("Live view ended.");
  }};

  player.attachMediaElement(video);
  player.load();

  await startPlayback();

  endTimer = setTimeout(() => {{
    endSession(`${{seconds}} second live view finished.`);
  }}, (seconds + 5) * 1000);
}}

restart.addEventListener("click", startPlayer);
save.addEventListener("click", saveLastView);
endButton.addEventListener("click", endCurrentStream);
sound.addEventListener("click", toggleSound);
video.addEventListener("volumechange", () => {{
  syncSoundButton();
  // Keep the preference in step with the native control bar too, so unmuting
  // there is remembered exactly like unmuting here. The one change that is
  // not a choice - the fallback to a muted start - is excluded.
  if (!restoringMute) rememberSound(!video.muted);
}});
talk.addEventListener("pointerdown", startTalk);
talk.addEventListener("pointerup", stopTalk);
talk.addEventListener("pointercancel", stopTalk);
talk.addEventListener("pointerleave", stopTalk);
video.addEventListener("loadedmetadata", positionLiveActions);
video.addEventListener("resize", positionLiveActions);
window.addEventListener("resize", positionLiveActions);
window.addEventListener("orientationchange", positionLiveActionsThroughRotation);
window.addEventListener("blur", stopTalk);
window.addEventListener("beforeunload", () => {{
  stopTalk();
}});
talk.hidden = !pttSupported;
syncSoundButton();

// The dialog closes by removing this frame. On iOS that alone left the
// <video> fetching HLS segments from a detached document, so the proxy never
// saw the stream go idle and kept the Blink live view open behind it. The
// parent calls this first; pagehide covers a normal navigation away.
window.__blinkStopPlayer = () => {{
  try {{ stopPlayer(); }} catch (err) {{}}
  // Closing the dialog should free the camera too, not leave it streaming to
  // nobody until the idle timeout elapses.
  stopUpstream();
}};
window.addEventListener("pagehide", () => {{
  window.__blinkStopPlayer();
}});

startPlayer();
</script>
</body>
</html>"""


class BlinkLiveviewProxyPlayerView(HomeAssistantView):
    """Serve a direct browser live-view player."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/cameras/{slug}/player"
    name = "api:blink_liveview_proxy:player"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, slug: str) -> web.Response:
        """Return the player HTML."""
        camera = _camera(self.hass, slug)
        access_token = _authorize_browser_request(
            self.hass, request, slug, issue_browser_token=True
        )
        return web.Response(
            text=_player_html(self.hass, slug, camera, access_token),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )


class BlinkLiveviewProxyBrowserTokenView(HomeAssistantView):
    """Mint a fresh browser token for a player page that has gone stale.

    A player left open outlives its token: Home Assistant rotates camera access
    tokens on a timer, and the browser tokens issued here expire on their own
    TTL. Nothing in the page noticed, so Restart replayed the dead token and
    every retry came back 403. A caller that can still prove it may watch this
    camera trades that proof for a new token here.

    Deliberately no self-service refresh: an expired token is not proof of
    anything, so it cannot mint its successor. The caller needs Home Assistant
    auth or the camera's current access token, which in practice means a
    credentialed server doing it on the page's behalf.
    """

    requires_auth = False
    url = "/api/blink_liveview_proxy/cameras/{slug}/token"
    name = "api:blink_liveview_proxy:token"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, slug: str) -> web.Response:
        """Issue a short-lived browser token scoped to one camera."""
        _camera(self.hass, slug)
        token = _authorize_browser_request(
            self.hass, request, slug, issue_browser_token=True
        )
        return web.json_response(
            {"token": token, "expires_in": BROWSER_TOKEN_TTL_SECONDS},
            headers={"Cache-Control": "no-store"},
        )


class BlinkLiveviewProxyMpegtsView(HomeAssistantView):
    """Proxy a raw MPEG-TS stream from the local proxy."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/cameras/{slug}/mpegts"
    name = "api:blink_liveview_proxy:mpegts"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, slug: str) -> web.StreamResponse:
        """Stream MPEG-TS to the browser."""
        _camera(self.hass, slug)
        _authorize_browser_request(self.hass, request, slug)
        query = {
            "seconds": request.query.get("seconds", str(_stream_seconds(self.hass))),
            "force": request.query.get("force", "1"),
            "session": request.query.get("session", ""),
        }
        return await _proxy_stream(
            self.hass,
            request,
            f"/cameras/{slug}/mpegts",
            "video/mp2t",
            query,
        )


class BlinkLiveviewProxyHlsPlaylistView(HomeAssistantView):
    """Serve the proxy's HLS playlist, rewritten so segments carry the token.

    iOS has no Media Source Extensions, so mpegts.js cannot run there at all.
    Native HLS is the only way an iPhone can play this stream. Segment URIs in
    the playlist are relative and players do not inherit the playlist's query
    string, so each one needs the browser token appended.
    """

    requires_auth = False
    url = "/api/blink_liveview_proxy/cameras/{slug}/hls/index.m3u8"
    name = "api:blink_liveview_proxy:hls_playlist"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, slug: str) -> web.Response:
        """Return the playlist with tokenised segment URIs."""
        _camera(self.hass, slug)
        _authorize_browser_request(self.hass, request, slug)
        query = {
            "seconds": request.query.get("seconds", str(_stream_seconds(self.hass))),
            "force": request.query.get("force", "1"),
            "session": request.query.get("session", ""),
        }
        upstream = await _open_proxy_response(
            _client(self.hass), f"/cameras/{slug}/hls/index.m3u8", query
        )
        try:
            text = await upstream.text()
        finally:
            upstream.release()

        return web.Response(
            text=tokenise_playlist(text, request.query.get("token", "")),
            content_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )


class BlinkLiveviewProxyHlsSegmentView(HomeAssistantView):
    """Proxy a single HLS segment from the local proxy."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/cameras/{slug}/hls/{filename}"
    name = "api:blink_liveview_proxy:hls_segment"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(
        self, request: web.Request, slug: str, filename: str
    ) -> web.StreamResponse:
        """Stream one segment to the browser."""
        # The filename goes straight into the upstream path, so it is pinned to
        # a bare .ts name. Without this a crafted segment URI could walk out of
        # the HLS directory.
        if "/" in filename or not filename.endswith(".ts"):
            raise web.HTTPNotFound()
        _camera(self.hass, slug)
        _authorize_browser_request(self.hass, request, slug)
        return await _proxy_stream(
            self.hass,
            request,
            f"/cameras/{slug}/hls/{filename}",
            "video/mp2t",
        )


class BlinkLiveviewProxyPttView(HomeAssistantView):
    """Proxy push-to-talk websocket audio to the local proxy."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/cameras/{slug}/ptt"
    name = "api:blink_liveview_proxy:ptt"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, slug: str) -> web.WebSocketResponse:
        """Bridge browser microphone audio to the local proxy websocket."""
        camera = _camera(self.hass, slug)
        if not bool(camera.get("ptt_supported", True)):
            raise web.HTTPBadRequest(text="Push-to-talk is not enabled for this camera\n")
        _authorize_browser_request(self.hass, request, slug)

        browser_ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024 * 1024)
        await browser_ws.prepare(request)

        session = request.query.get("session", "")
        if not session:
            await browser_ws.send_json(
                {"type": "error", "message": "Missing live-view session"}
            )
            await browser_ws.close()
            return browser_ws

        client = _client(self.hass)
        try:
            upstream_ws = await client._session.ws_connect(  # noqa: SLF001
                client.proxy_url(f"/cameras/{slug}/ptt", {"session": session}),
                headers=client.auth_headers(),
                timeout=ClientTimeout(connect=10, sock_connect=10, total=None),
                heartbeat=20,
                max_msg_size=1024 * 1024,
            )
        except ClientError as err:
            await browser_ws.send_json(
                {"type": "error", "message": f"PTT proxy failed: {err}"}
            )
            await browser_ws.close()
            return browser_ws

        async def browser_to_proxy() -> None:
            async for message in browser_ws:
                if message.type == WSMsgType.TEXT:
                    await upstream_ws.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await upstream_ws.send_bytes(message.data)
                elif message.type == WSMsgType.ERROR:
                    break

        async def proxy_to_browser() -> None:
            async for message in upstream_ws:
                if message.type == WSMsgType.TEXT:
                    await browser_ws.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await browser_ws.send_bytes(message.data)
                elif message.type == WSMsgType.ERROR:
                    break

        tasks = [
            asyncio.create_task(browser_to_proxy()),
            asyncio.create_task(proxy_to_browser()),
        ]
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        except (ConnectionResetError, ClientError):
            LOGGER.debug("Push-to-talk websocket closed for %s", slug)
        finally:
            await upstream_ws.close()
            await browser_ws.close()

        return browser_ws


class BlinkLiveviewProxyStopView(HomeAssistantView):
    """End a camera's live view now, rather than at the proxy's idle timeout."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/cameras/{slug}/stop"
    name = "api:blink_liveview_proxy:stop"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request, slug: str) -> web.Response:
        """Ask the proxy to stop this camera's session."""
        _camera(self.hass, slug)
        _authorize_browser_request(self.hass, request, slug)
        client = _client(self.hass)
        try:
            async with asyncio.timeout(15):
                async with client._session.post(  # noqa: SLF001
                    client.proxy_url(f"/cameras/{slug}/stop"),
                    headers=client.auth_headers(),
                ) as response:
                    payload = await response.json(content_type=None)
        except (ClientError, asyncio.TimeoutError, ValueError) as err:
            # Best effort by nature: the caller is on its way out either way,
            # and the idle timeout is still there behind this.
            LOGGER.debug("Could not stop the live view for %s: %s", slug, err)
            return web.json_response(
                {"stopped": False}, headers={"Cache-Control": "no-store"}
            )
        return web.json_response(
            payload if isinstance(payload, dict) else {"stopped": True},
            headers={"Cache-Control": "no-store"},
        )


class BlinkLiveviewProxyLastLiveviewInfoView(HomeAssistantView):
    """Proxy last-liveview metadata."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/cameras/{slug}/last-liveview"
    name = "api:blink_liveview_proxy:last_liveview"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, slug: str) -> web.Response:
        """Return cached live-view metadata."""
        _camera(self.hass, slug)
        _authorize_browser_request(self.hass, request, slug)
        upstream = await _open_proxy_response(
            _client(self.hass), f"/cameras/{slug}/last-liveview"
        )
        try:
            body = await upstream.read()
        finally:
            upstream.close()
        return web.Response(
            body=body,
            content_type="application/json",
            headers={"Cache-Control": "no-store"},
        )


class BlinkLiveviewProxyLastLiveviewDownloadView(HomeAssistantView):
    """Proxy the last cached live-view download."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/cameras/{slug}/last-liveview.ts"
    name = "api:blink_liveview_proxy:last_liveview_download"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, slug: str) -> web.StreamResponse:
        """Download the last cached live-view MPEG-TS file."""
        _camera(self.hass, slug)
        _authorize_browser_request(self.hass, request, slug)
        return await _proxy_stream(
            self.hass,
            request,
            f"/cameras/{slug}/last-liveview.ts",
            "video/mp2t",
            download_filename=f"{slug}_last_liveview.ts",
        )


class BlinkLiveviewProxyLastLiveviewMp4DownloadView(HomeAssistantView):
    """Proxy the last cached live-view MP4 download."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/cameras/{slug}/last-liveview.mp4"
    name = "api:blink_liveview_proxy:last_liveview_mp4_download"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, slug: str) -> web.StreamResponse:
        """Download the last cached live-view as an MP4 file."""
        _camera(self.hass, slug)
        _authorize_browser_request(self.hass, request, slug)
        return await _proxy_stream(
            self.hass,
            request,
            f"/cameras/{slug}/last-liveview.mp4",
            "video/mp4",
        )


class BlinkLiveviewProxySnapshotRefreshView(HomeAssistantView):
    """Ask Home Assistant's normal Blink camera entity for a fresh snapshot."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/cameras/{slug}/snapshot-refresh"
    name = "api:blink_liveview_proxy:snapshot_refresh"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, slug: str) -> web.Response:
        """Refresh the source Blink snapshot."""
        return await self._refresh(request, slug)

    async def post(self, request: web.Request, slug: str) -> web.Response:
        """Refresh the source Blink snapshot."""
        return await self._refresh(request, slug)

    async def _refresh(self, request: web.Request, slug: str) -> web.Response:
        camera = _camera(self.hass, slug)
        _authorize_browser_request(self.hass, request, slug)
        source_entity_id = str(camera.get("entity_id") or "")
        if not source_entity_id:
            raise web.HTTPNotFound(text="Camera has no source Blink entity\n")

        try:
            await self.hass.services.async_call(
                "blink",
                "trigger_camera",
                {"entity_id": source_entity_id},
                blocking=True,
            )
        except ServiceNotFound as err:
            # This is the one feature here that genuinely needs the official
            # Blink integration: it owns blink.trigger_camera. Say that,
            # instead of raising a 500 that reads like the proxy is broken.
            raise web.HTTPNotFound(
                text=(
                    "Snapshot refresh needs the official Blink integration, "
                    "which provides the blink.trigger_camera service. Live "
                    "view, clips and push-to-talk do not.\n"
                )
            ) from err
        await asyncio.sleep(1)
        await self.hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": source_entity_id},
            blocking=True,
        )
        cache = str(int(time.time() * 1000))
        return web.json_response(
            {
                "ok": True,
                "slug": slug,
                "entity_id": source_entity_id,
                "snapshot_url": _snapshot_url(self.hass, source_entity_id, cache),
            },
            headers={"Cache-Control": "no-store"},
        )


def _rewrite_clip_download_urls(
    payload: dict[str, Any], access_token: str = ""
) -> dict[str, Any]:
    """Rewrite proxy-relative clip URLs into authenticated HA API URLs.

    Both the clip and, from proxy 0.7.0, its thumbnail. An older proxy sends
    no thumbnail_url and the viewer shows a placeholder for that row.
    """
    for clip in payload.get("clips", []):
        if not isinstance(clip, dict):
            continue
        for key in ("download_url", "thumbnail_url"):
            url = str(clip.get(key) or "")
            if not url:
                continue
            if url.startswith("/clips/"):
                url = f"/api/blink_liveview_proxy{url}"
            if access_token:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}token={quote(access_token, safe='')}"
            clip[key] = url
    return payload


def _clips_viewer_html(
    camera_slug: str | None,
    access_token: str,
    cameras: list[dict[str, str]] | None = None,
) -> str:
    """Return the clip viewer page, for Sync Module and cloud clips alike.

    Two panes that scroll independently: the list on its own, and the
    player locked to the viewport beside it. A plain string, not an
    f-string, because the CSS is full of braces; values go in by
    .replace(), and a test checks every placeholder is substituted.
    """
    camera_json = json.dumps(camera_slug or "")
    cameras_json = json.dumps(cameras or [])
    token_json = json.dumps(access_token)
    html_text = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Clips</title>
<link rel="icon" href="__ASSET_BASE__/icon.png">
<style>
:root{color-scheme:dark;--bg:#05070a;--panel:#0b1018;--card:#111827;--line:rgba(148,163,184,.16);--text:#f8fafc;--muted:#cbd5e1;--dim:#94a3b8;--accent:#0284c7;--accent-2:#38bdf8}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font-family:Arial,Helvetica,sans-serif}
/* The page is locked to the viewport and the list scrolls on its own. It used
   to grow with the list, so the preview stretched to the height of sixty rows
   and the video sat somewhere in the middle of it. */
body{height:100vh;height:100dvh;display:grid;grid-template-rows:auto minmax(0,1fr);overflow:hidden}
.toolbar{display:flex;flex-wrap:wrap;align-items:end;gap:10px 14px;padding:10px 14px;background:var(--panel);border-bottom:1px solid var(--line)}
.toolbar h1{display:none;margin:0;font-size:17px;align-self:center}
body.standalone .toolbar h1{display:block}
/* Opened in the dialog there is a floating close button over the top-left
   corner, so the first control has to start clear of it. Standalone there is
   no such button and the heading takes that space instead. */
body:not(.standalone) .toolbar{padding-left:60px}
label{display:grid;gap:4px;color:var(--dim);font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}
select,button,a.button{min-height:34px;border:1px solid rgba(148,163,184,.28);border-radius:6px;background:var(--card);color:var(--text);font:inherit;font-size:14px}
select{min-width:128px;padding:0 10px}
button,a.button{display:inline-grid;place-items:center;padding:0 12px;font-weight:700;cursor:pointer;text-decoration:none}
button.primary{border-color:var(--accent);background:var(--accent)}
button:disabled{opacity:.6;cursor:default}
.summary{margin-left:auto;align-self:center;color:var(--dim);font-size:13px}
.badge{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:999px;border:1px solid var(--line);color:var(--dim);font-size:11px;font-weight:600;vertical-align:middle}
.thumb.cloud .fallback{display:grid;font-size:11px;font-weight:600;text-align:center;line-height:1.25;padding:0 6px}
main{display:grid;grid-template-columns:minmax(300px,400px) minmax(0,1fr);min-height:0}
.list{overflow-y:auto;overscroll-behavior:contain;border-right:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom,0px)}
.empty,.loading{padding:28px 18px;color:var(--muted);line-height:1.5}
.clip{display:grid;grid-template-columns:132px minmax(0,1fr);gap:12px;align-items:center;width:100%;margin:0;padding:10px 12px;border:0;border-bottom:1px solid var(--line);border-radius:0;background:transparent;color:inherit;text-align:left;cursor:pointer;font:inherit}
.clip:hover,.clip:focus-visible{background:rgba(148,163,184,.08);outline:none}
.clip.active{background:rgba(2,132,199,.16);box-shadow:inset 3px 0 0 var(--accent-2)}
/* The tile is its own placeholder. aspect-ratio plus the img's width/height
   attributes mean the row is the right height before anything is fetched, so
   nothing moves when the picture arrives - it fades in over the skeleton.
   That replaced a spinner, which was both easy to miss at this size and the
   only thing between an empty tile and a filled one. */
/* There is no <img> here on purpose. The picture arrives as a background on
   a div of fixed size, and a background image takes no part in layout at all
   - so the row cannot change height when it loads, whatever the picture turns
   out to be. With an img the tile took its height from the one child whose
   size is unknown until the network answers, and a list of cold rows was a
   stack of squat rows that each jumped to full height as its own picture
   landed. The tile is its final size from the first paint. */
.thumb{position:relative;width:132px;height:74px;border-radius:6px;overflow:hidden;background:#1b2635}
.thumb .picture{position:absolute;inset:0;background-position:center;background-size:cover;background-repeat:no-repeat;opacity:0;transition:opacity .3s ease}
.thumb.loaded .picture{opacity:1}
/* Two channels, because one subtle one is not enough: the whole tile pulses
   between two clearly separated blues, and a light band sweeps across it.
   The first version moved #141c26 to #202c3a - a step of about fifteen per
   channel on a 132px tile - which animated correctly and could not be seen. */
.thumb .skeleton{position:absolute;inset:0;background:#1b2635;animation:pulse 1.3s ease-in-out infinite}
.thumb .skeleton::after{content:"";position:absolute;inset:0;background:linear-gradient(100deg,transparent 32%,rgba(125,211,252,.22) 50%,transparent 68%);background-size:220% 100%;animation:sweep 1.3s linear infinite}
.thumb.loaded .skeleton,.thumb.failed .skeleton,.thumb.none .skeleton{display:none}
@keyframes pulse{0%,100%{background-color:#1b2635}50%{background-color:#31465f}}
@keyframes sweep{from{background-position:170% 0}to{background-position:-70% 0}}
.thumb .fallback{position:absolute;inset:0;display:none;place-items:center;color:var(--dim)}
.thumb.failed .fallback,.thumb.none .fallback{display:grid}
.thumb svg{width:28px;height:28px;fill:currentColor}
.thumb .play{position:absolute;inset:0;display:grid;place-items:center;opacity:0;transition:opacity .15s ease;background:rgba(2,6,23,.35)}
.thumb .play svg{width:34px;height:34px;filter:drop-shadow(0 2px 6px rgba(0,0,0,.6))}
.clip:hover .thumb.loaded .play,.clip:focus-visible .thumb.loaded .play,.clip.active .thumb.loaded .play{opacity:1}
.text{min-width:0;display:grid;gap:3px}
.text strong{font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.meta{color:var(--muted);font-size:13px}
.stage{display:grid;grid-template-rows:minmax(0,1fr) auto;min-height:0;gap:12px;padding:16px;background:#020617}
/* The video fills whatever the stage gives it and letterboxes inside, so it
   is centred whether the row is tall or wide. No fixed frame: a box sized by
   aspect-ratio cannot shrink on both axes at once. */
.frame{position:relative;min-height:0;display:grid;place-items:center;border-radius:10px;overflow:hidden;background:#000}
video{display:block;width:100%;height:100%;max-height:100%;object-fit:contain;background:#000}
.placeholder{position:absolute;inset:0;display:grid;place-content:center;justify-items:center;gap:10px;padding:24px;color:var(--dim);text-align:center;line-height:1.5}
.placeholder svg{width:52px;height:52px;fill:currentColor;opacity:.6}
.frame.playing .placeholder{display:none}
.now{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;min-height:34px}
.now strong{font-size:15px}
.now .meta{flex:1 1 auto}
.now[hidden]{display:none}
/* Still obviously a placeholder without moving: a flat fill lighter than the
   tile it sits in, rather than nothing at all. */
@media (prefers-reduced-motion:reduce){
  .thumb .skeleton{animation:none;background:#2b3d53}
  .thumb .skeleton::after{display:none}
}
@media (max-width:780px){
  /* Phone: video first at its natural height, the list scrolls underneath. */
  main{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr)}
  .stage{padding:10px 10px 8px;gap:8px}
  .frame{aspect-ratio:16/9}
  video{height:100%}
  .list{border-right:0;border-top:1px solid var(--line)}
  .clip{grid-template-columns:104px minmax(0,1fr)}
  .thumb{width:104px;height:59px}
  .toolbar{gap:8px 10px}
  select{min-width:104px}
}
</style>
</head>
<body>
<section class="toolbar">
  <h1>Clips</h1>
  <label>Source
    <select id="source">
      <option value="both" selected>Sync Module + cloud</option>
      <option value="cloud">Blink cloud</option>
      <option value="local">Sync Module</option>
    </select>
  </label>
  <label>Window
    <select id="hours">
      <option value="24">24 hours</option>
      <option value="72">3 days</option>
      <option value="168" selected>7 days</option>
      <option value="720">30 days</option>
    </select>
  </label>
  <label>Camera
    <select id="camera">
      <option value="">All cameras</option>
    </select>
  </label>
  <label>Show
    <select id="limit">
      <option value="30">30 clips</option>
      <option value="60" selected>60 clips</option>
      <option value="100">100 clips</option>
    </select>
  </label>
  <button id="refresh" class="primary" type="button">Refresh</button>
  <button id="cloudThumbs" class="secondary" type="button" hidden>Load cloud thumbnails</button>
  <span id="summary" class="summary"></span>
</section>
<main>
  <section class="stage">
    <div id="frame" class="frame">
      <video id="video" controls playsinline preload="metadata"></video>
      <div class="placeholder">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18,4L20,8H17L15,4H13L15,8H12L10,4H8L10,8H7L5,4H4A2,2 0 0,0 2,6V18A2,2 0 0,0 4,20H20A2,2 0 0,0 22,18V4H18Z"/></svg>
        <span>Select a clip to play it here.</span>
      </div>
    </div>
    <div id="now" class="now" hidden>
      <strong id="nowTitle"></strong>
      <span id="nowMeta" class="meta"></span>
      <a id="nowDownload" class="button" href="#" download>Download</a>
    </div>
  </section>
  <section id="list" class="list" role="list" aria-label="Clips">
    <div class="loading">Loading clips…</div>
  </section>
</main>
<script>
const list = document.getElementById("list");
const video = document.getElementById("video");
const frame = document.getElementById("frame");
const summary = document.getElementById("summary");
const now = document.getElementById("now");
const nowTitle = document.getElementById("nowTitle");
const nowMeta = document.getElementById("nowMeta");
const nowDownload = document.getElementById("nowDownload");
const source = document.getElementById("source");
const hours = document.getElementById("hours");
const limit = document.getElementById("limit");
const camera = document.getElementById("camera");
const refresh = document.getElementById("refresh");
const cloudThumbs = document.getElementById("cloudThumbs");
const initial = new URLSearchParams(window.location.search);
const fixedCamera = __CAMERA_JSON__;
const inventory = __CAMERAS_JSON__;
const TOKENS_REQUEST = "blink_liveview_proxy_clips_tokens";
const TOKENS_REPLY = "blink_liveview_proxy_clips_tokens_reply";
const SOURCE_KEY = "blink_liveview_proxy.clips.source";
const EMPTY_TEXT = {
  both: "No clips in this window, from the Sync Module or from Blink's cloud. Try a longer one.",
  cloud: "No clips in this window from Blink's cloud. Try a longer one, or another source.",
  local: "No clips in this window from the Sync Module. Try a longer one, or another source."
};
let loadSeq = 0;
const accessToken = __TOKEN_JSON__;
const FILM_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18,4L20,8H17L15,4H13L15,8H12L10,4H8L10,8H7L5,4H4A2,2 0 0,0 2,6V18A2,2 0 0,0 4,20H20A2,2 0 0,0 22,18V4H18Z"/></svg>';
const PLAY_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M8,5.14V19.14L19,12.14L8,5.14Z"/></svg>';
let clips = [];
let activeId = "";

// Opened outside the dialog there is no header naming the page, so show one.
if (window.self === window.top) document.body.classList.add("standalone");

// An account whose clips are all in one place would otherwise pick it again every open.
let remembered = "";
try { remembered = localStorage.getItem(SOURCE_KEY) || ""; } catch (err) { remembered = ""; }
const wantedSource = initial.get("source") || remembered;
if (["both", "cloud", "local"].includes(wantedSource)) source.value = wantedSource;

// Each request is authorised for one camera; inside the dialog the page borrows the token of every other one.
const inDialog = Boolean(fixedCamera) && window.self !== window.top;
const tokens = fixedCamera && accessToken ? { [fixedCamera]: accessToken } : {};
let chosen = false;

function fillCameraOptions() {
  const known = new Map(inventory.map((item) => [item.slug, item.name || item.slug]));
  const wantedCamera = fixedCamera ? "" : initial.get("camera") || "";
  if (wantedCamera && !known.has(wantedCamera)) known.set(wantedCamera, wantedCamera);
  const pinned = Boolean(fixedCamera) && Object.keys(tokens).length < 2;
  const slugs = fixedCamera ? Object.keys(tokens) : [...known.keys()];
  const current = camera.value;
  camera.replaceChildren();
  if (!pinned) camera.append(new Option("All cameras", ""));
  slugs.sort((a, b) => (known.get(a) || a).localeCompare(known.get(b) || b));
  for (const slug of slugs) camera.append(new Option(known.get(slug) || slug, slug));
  let value = "";
  if (pinned) value = fixedCamera;
  else if (chosen) value = slugs.includes(current) ? current : "";
  else if (!fixedCamera) value = wantedCamera;
  camera.value = value;
  camera.disabled = pinned;
}
fillCameraOptions();

// Home Assistant rotates camera tokens, so the dialog is asked again before every load; a dialog that never answers (an older copy still cached) leaves the page pinned.
function requestTokens() {
  return new Promise((resolve) => {
    if (!inDialog) { resolve(); return; }
    const timer = setTimeout(() => { window.removeEventListener("message", onReply); resolve(); }, 1500);
    function onReply(event) {
      const data = event.data || {};
      if (event.source !== window.parent || event.origin !== window.location.origin || data.type !== TOKENS_REPLY) return;
      window.removeEventListener("message", onReply);
      clearTimeout(timer);
      Object.assign(tokens, data.tokens || {});
      resolve();
    }
    window.addEventListener("message", onReply);
    window.parent.postMessage({ type: TOKENS_REQUEST }, window.location.origin);
  });
}

// Thumbnails are cut on the proxy from a clip it has to fetch from Blink
// first, one at a time - each fetch makes the Sync Module upload the clip to
// Blink's cloud and polls until it lands, about two and a half seconds.
//
// Asking for every visible row at once therefore put a dozen requests behind
// that one queue. The last of them waited the better part of a minute, some
// came back 502, and Blink began throttling the burst of prepare_download
// calls. So the browser keeps its own queue two deep, rows fill top-down, and
// a request that fails anyway is retried rather than left as a dead tile.
const QUEUE = [];
let inFlight = 0;
const MAX_IN_FLIGHT = 2;

function pump() {
  while (inFlight < MAX_IN_FLIGHT && QUEUE.length) {
    const job = QUEUE.shift();
    inFlight += 1;
    job().then(() => { inFlight -= 1; pump(); });
  }
}

function loadThumbnail(box, picture, url, attempt = 0) {
  return new Promise((resolve) => {
    // A detached Image is only ever a loader. It never enters the document,
    // so it cannot affect layout; once it has decoded, the same URL goes on
    // as a background and fades in.
    const probe = new Image();
    probe.onload = () => {
      picture.style.backgroundImage = `url("${probe.src.replace(/"/g, "%22")}")`;
      box.classList.add("loaded");
      resolve();
    };
    probe.onerror = () => {
      // Transient by nature: the proxy may still be signing in to Blink, or
      // this clip lost its place behind a long queue. Both used to leave a
      // placeholder that never recovered.
      if (attempt < 2) {
        setTimeout(
          () => loadThumbnail(box, picture, url, attempt + 1).then(resolve),
          1200 * (attempt + 1),
        );
        return;
      }
      box.classList.add("failed");
      resolve();
    };
    // A fresh query string per attempt: a browser will not re-request a src it
    // has already failed on.
    probe.src = attempt ? `${url}${url.includes("?") ? "&" : "?"}retry=${attempt}` : url;
  });
}

function queueThumbnail(box, picture, url) {
  QUEUE.push(() => loadThumbnail(box, picture, url));
  pump();
}

const visible = "IntersectionObserver" in window
  ? new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const box = entry.target;
        visible.unobserve(box);
        queueThumbnail(box, box.querySelector(".picture"), box.dataset.src);
      }
    }, { root: list, rootMargin: "160px 0px" })
  : null;

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function formatSize(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size <= 0) return "";
  if (size < 1024) return `${Math.round(size)} KB`;
  return `${(size / 1024).toFixed(1)} MB`;
}

function optionLabel(clip) {
  return clip.camera_name || clip.slug || "Camera";
}

// What a cloud thumbnail actually costs.
//
// A thumbnail is the first frame cut from the clip file, so there is no cheap
// way to draw one: the clip has to be on disk first. For a local clip that
// download is the Sync Module on your own network. For a cloud clip it is
// Blink's servers, and a screenful of tiles would pull every clip in the
// window off them - so cloud tiles stay placeholders until someone asks,
// either by playing one clip or by pressing the button, which says what it is
// about to do. Blink's own app behaves the same way: a list is cheap, a clip
// is fetched when you tap it.
//
// The newest few are drawn anyway: a screen of grey tiles says nothing about
// what you are choosing between, and six is about one phone screen of them.
const AUTO_CLOUD_THUMBNAILS = 6;
let cloudThumbnailsOn = false;
const fetchedClips = new Set();
const autoThumbs = new Set();

function cloudThumbnailReady(clip) {
  return cloudThumbnailsOn || fetchedClips.has(clip.id) || autoThumbs.has(clip.id);
}

function thumbnail(clip) {
  const box = document.createElement("div");
  box.className = "thumb";
  const fallback = document.createElement("div");
  fallback.className = "fallback";
  fallback.innerHTML = FILM_ICON;
  box.append(fallback);
  if (!clip.thumbnail_url) {
    // An older proxy lists clips without thumbnails. Say so quietly.
    box.classList.add("none");
    return box;
  }
  if (clip.source === "cloud" && !cloudThumbnailReady(clip)) {
    box.classList.add("none", "cloud");
    fallback.textContent = "In Blink's cloud";
    box.title = "Play this clip, or load cloud thumbnails, to see a frame";
    return box;
  }
  const skeleton = document.createElement("div");
  skeleton.className = "skeleton";
  skeleton.setAttribute("aria-label", "Loading thumbnail");
  const picture = document.createElement("div");
  picture.className = "picture";
  const play = document.createElement("div");
  play.className = "play";
  play.innerHTML = PLAY_ICON;
  box.append(skeleton, picture, play);
  box.dataset.src = clip.thumbnail_url;
  if (visible) visible.observe(box);
  else queueThumbnail(box, picture, clip.thumbnail_url);
  return box;
}

function upgradeThumbnail(clip) {
  // Swap one placeholder for a real tile without rebuilding the list, which
  // would move the scroll position out from under whoever just clicked.
  const row = list.querySelector(`.clip[data-id="${CSS.escape(clip.id)}"]`);
  const existing = row && row.querySelector(".thumb");
  if (!existing) return;
  existing.replaceWith(thumbnail(clip));
}

function play(clip) {
  activeId = clip.id;
  if (clip.source === "cloud" && !fetchedClips.has(clip.id)) {
    // Playing it puts it in the proxy's cache, so the thumbnail now costs
    // nothing more. The proxy locks per clip, so this waits on the same
    // download rather than starting a second one.
    fetchedClips.add(clip.id);
    updateCloudThumbnailButton();
    upgradeThumbnail(clip);
  }
  for (const row of list.querySelectorAll(".clip")) {
    row.classList.toggle("active", row.dataset.id === clip.id);
  }
  frame.classList.add("playing");
  video.src = clip.download_url;
  video.load();
  video.play().catch(() => {});
  nowTitle.textContent = optionLabel(clip);
  const size = formatSize(clip.size);
  nowMeta.textContent = `${formatTime(clip.created_at)}${size ? ` · ${size}` : ""}`;
  nowDownload.href = clip.download_url;
  now.hidden = false;
}

function shownClips() {
  const selected = camera.value;
  const shown = selected ? clips.filter((clip) => clip.slug === selected) : clips;
  autoThumbs.clear();
  for (const clip of shown) {
    if (autoThumbs.size >= AUTO_CLOUD_THUMBNAILS) break;
    if (clip.source === "cloud" && clip.thumbnail_url) autoThumbs.add(clip.id);
  }
  return shown;
}

function render() {
  const shown = shownClips();
  summary.textContent = shown.length ? `${shown.length} clip${shown.length === 1 ? "" : "s"}` : "";
  QUEUE.length = 0;
  list.replaceChildren();
  if (!shown.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = EMPTY_TEXT[source.value] || EMPTY_TEXT.both;
    list.append(empty);
    return;
  }
  for (const clip of shown) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `clip${clip.id === activeId ? " active" : ""}`;
    row.dataset.id = clip.id;
    row.setAttribute("role", "listitem");
    row.setAttribute("aria-label", `${optionLabel(clip)}, ${formatTime(clip.created_at)}`);
    const text = document.createElement("div");
    text.className = "text";
    const title = document.createElement("strong");
    title.textContent = optionLabel(clip);
    if (clip.source === "cloud") {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "Cloud";
      title.append(badge);
    }
    const meta = document.createElement("div");
    meta.className = "meta";
    const size = formatSize(clip.size);
    meta.textContent = `${formatTime(clip.created_at)}${size ? ` · ${size}` : ""}`;
    text.append(title, meta);
    row.append(thumbnail(clip), text);
    row.addEventListener("click", () => play(clip));
    list.append(row);
  }
}

function pendingCloudThumbnails() {
  return shownClips().filter(
    (clip) => clip.source === "cloud" && clip.thumbnail_url && !cloudThumbnailReady(clip)
  );
}

function updateCloudThumbnailButton() {
  cloudThumbs.hidden = cloudThumbnailsOn || pendingCloudThumbnails().length === 0;
}

function loadCloudThumbnails() {
  const pending = pendingCloudThumbnails();
  if (!pending.length) return;
  const count = pending.length;
  // Spelled out rather than softened: this is the one action here that pulls
  // video off Blink's servers without anyone having asked for that clip.
  const warning =
    `Load ${count} cloud thumbnail${count === 1 ? "" : "s"}?\n\n` +
    "A thumbnail is the first frame of the clip, so each one has to be " +
    `downloaded from Blink first — ${count} clip${count === 1 ? "" : "s"}, ` +
    "kept in the proxy's cache afterwards. Clips already on your Sync Module " +
    "are not affected.";
  if (!window.confirm(warning)) return;
  cloudThumbnailsOn = true;
  updateCloudThumbnailButton();
  render();
}

async function fetchClips(params) {
  const response = await fetch(`/api/blink_liveview_proxy/clips?${params}`, {
    cache: "no-store",
    credentials: "same-origin"
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data = await response.json();
  return Array.isArray(data.clips) ? data.clips : [];
}

// A standalone page is authenticated by Home Assistant itself; inside the dialog every camera is listed on its own token and the lists merged.
async function listClips(base) {
  if (!fixedCamera) {
    const params = new URLSearchParams(base);
    if (camera.value) params.set("camera", camera.value);
    return fetchClips(params);
  }
  const results = await Promise.allSettled(Object.entries(tokens).map(([slug, token]) => {
    const params = new URLSearchParams(base);
    params.set("camera", slug);
    params.set("token", token);
    return fetchClips(params);
  }));
  const merged = results.filter((item) => item.status === "fulfilled").flatMap((item) => item.value);
  const failed = results.find((item) => item.status === "rejected");
  if (!merged.length && failed) throw failed.reason;
  merged.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  return merged.slice(0, Number(limit.value) || merged.length);
}

async function loadClips() {
  const seq = ++loadSeq;
  refresh.disabled = true;
  list.innerHTML = '<div class="loading">Loading clips…</div>';
  await requestTokens();
  if (seq !== loadSeq) return;
  fillCameraOptions();
  try {
    const loaded = await listClips({
      hours: hours.value,
      limit: limit.value,
      // Listing is metadata only - no clip is fetched to build this list.
      source: source.value
    });
    // A slower reply from an earlier load must not overwrite the current one.
    if (seq !== loadSeq) return;
    clips = loaded;
  } catch (err) {
    if (seq !== loadSeq) return;
    list.innerHTML = '<div class="empty">Could not load clips. Check that the proxy is running and signed in to Blink, then refresh.</div>';
    summary.textContent = "";
    refresh.disabled = false;
    return;
  }
  updateCloudThumbnailButton();
  render();
  refresh.disabled = false;
}

// Arrow keys move through the list from wherever focus is; Enter/Space on a
// row already plays it, because rows are buttons.
document.addEventListener("keydown", (event) => {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const rows = [...list.querySelectorAll(".clip")];
  if (!rows.length) return;
  const index = rows.findIndex((row) => row.dataset.id === activeId);
  const next = rows[Math.min(rows.length - 1, Math.max(0, index + (event.key === "ArrowDown" ? 1 : -1)))];
  if (!next) return;
  event.preventDefault();
  next.focus();
  next.click();
});

refresh.addEventListener("click", loadClips);
cloudThumbs.addEventListener("click", loadCloudThumbnails);
source.addEventListener("change", () => {
  try { localStorage.setItem(SOURCE_KEY, source.value); } catch (err) { /* private browsing */ }
  loadClips();
});
hours.addEventListener("change", loadClips);
limit.addEventListener("change", loadClips);
camera.addEventListener("change", () => {
  chosen = true;
  updateCloudThumbnailButton();
  render();
});
loadClips();
</script>
</body>
</html>"""
    return (
        html_text.replace("__CAMERA_JSON__", camera_json)
        .replace("__CAMERAS_JSON__", cameras_json)
        .replace("__ASSET_BASE__", ASSET_URL_BASE)
        .replace("__TOKEN_JSON__", token_json)
    )


def _clip_query(request: web.Request, *, allow_both: bool) -> dict[str, str]:
    """The listing arguments a clip request may carry, and its clip source.

    `source` decides which Blink inventory a clip is looked for in. It used to
    be pinned to "local" here, which is why an account whose clips are all in
    Blink's cloud saw an empty viewer. It is an enum, checked against the
    values the proxy accepts, and never passed through as free text.
    """
    allowed = {"camera", "hours", "pages", "limit"}
    query = {
        key: value for key, value in request.query.items() if key in allowed
    }
    source = request.query.get("source", "local")
    permitted = {"local", "cloud", "both"} if allow_both else {"local", "cloud"}
    if source not in permitted:
        raise web.HTTPBadRequest(text="Unknown clip source\n")
    query["source"] = source
    return query


class BlinkLiveviewProxyClipsView(HomeAssistantView):
    """Proxy recent Blink clip metadata."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/clips"
    name = "api:blink_liveview_proxy:clips"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Return recent clip metadata from the local proxy."""
        camera_slug = request.query.get("camera") or None
        if camera_slug:
            _camera(self.hass, camera_slug)
            _authorize_browser_request(self.hass, request, camera_slug)
        elif not request.get(KEY_AUTHENTICATED, False):
            raise web.HTTPForbidden(text="Missing camera token\n")

        query = _clip_query(request, allow_both=True)
        upstream = await _open_proxy_response(_client(self.hass), "/clips", query)
        try:
            body = await upstream.read()
        finally:
            upstream.close()
        try:
            payload = _rewrite_clip_download_urls(
                json.loads(body), request.query.get("token", "")
            )
        except (TypeError, ValueError):
            return web.Response(
                body=body,
                content_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            payload,
            headers={"Cache-Control": "no-store"},
        )


class BlinkLiveviewProxyClipDownloadView(HomeAssistantView):
    """Proxy one clip download, from the Sync Module or from Blink's cloud."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/clips/{clip_id}.mp4"
    name = "api:blink_liveview_proxy:clip_download"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, clip_id: str) -> web.StreamResponse:
        """Download one clip."""
        camera_slug = request.query.get("camera") or None
        if camera_slug:
            _camera(self.hass, camera_slug)
            _authorize_browser_request(self.hass, request, camera_slug)
        elif not request.get(KEY_AUTHENTICATED, False):
            raise web.HTTPForbidden(text="Missing camera token\n")

        query = _clip_query(request, allow_both=False)
        return await _proxy_stream(
            self.hass,
            request,
            f"/clips/{clip_id}.mp4",
            "video/mp4",
            query,
            cache_control="private, max-age=86400",
        )


class BlinkLiveviewProxyClipThumbnailView(HomeAssistantView):
    """Proxy one clip's first frame. The proxy cuts and keeps it.

    Same authorization as the clip itself. A proxy older than 0.7.0 has no
    such route and answers 404, which the viewer turns into a placeholder.
    """

    requires_auth = False
    url = "/api/blink_liveview_proxy/clips/{clip_id}.jpg"
    name = "api:blink_liveview_proxy:clip_thumbnail"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, clip_id: str) -> web.StreamResponse:
        """Return one clip thumbnail."""
        camera_slug = request.query.get("camera") or None
        if camera_slug:
            _camera(self.hass, camera_slug)
            _authorize_browser_request(self.hass, request, camera_slug)
        elif not request.get(KEY_AUTHENTICATED, False):
            raise web.HTTPForbidden(text="Missing camera token\n")

        query = _clip_query(request, allow_both=False)
        return await _proxy_stream(
            self.hass,
            request,
            f"/clips/{clip_id}.jpg",
            "image/jpeg",
            query,
            cache_control="private, max-age=86400",
        )


class BlinkLiveviewProxyClipsViewerView(HomeAssistantView):
    """Serve the clips viewer."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/clips/viewer"
    name = "api:blink_liveview_proxy:clips_viewer"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Return the clips viewer HTML."""
        camera_slug = request.query.get("camera") or None
        access_token = ""
        if camera_slug:
            _camera(self.hass, camera_slug)
            access_token = _authorize_browser_request(
                self.hass, request, camera_slug, issue_browser_token=True
            )
        elif not request.get(KEY_AUTHENTICATED, False):
            raise web.HTTPForbidden(text="Missing camera token\n")

        return web.Response(
            text=_clips_viewer_html(camera_slug, access_token, _camera_inventory(self.hass)),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )


AUTH_VIEW_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class BlinkLiveviewProxyAuthStatusView(HomeAssistantView):
    """Expose safe proxy auth state to authenticated HA administrators."""

    requires_auth = True
    url = "/api/blink_liveview_proxy/auth/status"
    name = "api:blink_liveview_proxy:auth_status"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @require_admin
    async def get(self, _request: web.Request) -> web.Response:
        """Return the redacted authentication state, or why there is none.

        A failure is reported as a state, not an HTTP error: the panel's job is
        to say what is wrong, and a 502 with no body leaves it guessing.
        """
        try:
            payload = await _auth_client(self.hass).async_get_auth_status()
        except Exception as err:  # noqa: BLE001 - redact all upstream details
            payload = _auth_failure_payload(err)
        return web.json_response(payload, headers=AUTH_VIEW_HEADERS)


class BlinkLiveviewProxyAuthActionView(HomeAssistantView):
    """Forward credential bodies to proxy auth endpoints for HA admins only."""

    requires_auth = True
    url = "/api/blink_liveview_proxy/auth/{action}"
    name = "api:blink_liveview_proxy:auth_action"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @require_admin
    async def post(self, request: web.Request, action: str) -> web.Response:
        """Run login, PIN submission, or cancellation without logging bodies."""
        if action not in {"login", "pin", "cancel"}:
            raise web.HTTPNotFound()
        if request.content_length is not None and request.content_length > 4096:
            raise web.HTTPRequestEntityTooLarge(
                max_size=4096, actual_size=request.content_length
            )
        try:
            body = await request.json()
        except Exception as err:  # noqa: BLE001 - never reflect parser/request details
            raise web.HTTPBadRequest(
                text="Invalid authentication request\n", headers=AUTH_VIEW_HEADERS
            ) from err
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(
                text="Invalid authentication request\n", headers=AUTH_VIEW_HEADERS
            )

        client = _auth_client(self.hass)
        try:
            if action == "login":
                payload = await client.async_start_auth(
                    str(body.get("username") or ""),
                    str(body.get("password") or ""),
                )
            elif action == "pin":
                payload = await client.async_submit_auth_pin(
                    str(body.get("challenge_id") or ""),
                    str(body.get("pin") or ""),
                )
            else:
                payload = await client.async_cancel_auth(
                    str(body.get("challenge_id") or "")
                )
        except Exception as err:  # noqa: BLE001 - redact all upstream details
            # The body carries the classified reason even though the status is
            # an error, so a panel that reads it can be specific either way.
            raise web.HTTPBadGateway(
                text=json.dumps(_auth_failure_payload(err)),
                content_type="application/json",
                headers=AUTH_VIEW_HEADERS,
            ) from err
        return web.json_response(payload, status=202, headers=AUTH_VIEW_HEADERS)


def _entity_summary(hass: HomeAssistant, entity_entry: Any) -> dict[str, Any]:
    """Return only safe, useful fields for one native HA entity."""
    entity_id = str(entity_entry.entity_id)
    state = hass.states.get(entity_id)
    attributes = state.attributes if state else {}
    return {
        "entity_id": entity_id,
        "domain": entity_id.partition(".")[0],
        "name": str(
            attributes.get("friendly_name")
            or getattr(entity_entry, "original_name", None)
            or entity_id
        ),
        "state": str(state.state) if state else "unavailable",
        "device_class": str(attributes.get("device_class") or ""),
        "unit": str(attributes.get("unit_of_measurement") or ""),
        "icon": str(attributes.get("icon") or ""),
        "disabled": getattr(entity_entry, "disabled_by", None) is not None,
    }


def _panel_cameras(hass: HomeAssistant, runtime: dict[str, Any]) -> list[dict[str, Any]]:
    """Join proxy cameras to their official Blink device entities."""
    from homeassistant.helpers import entity_registry as er

    coordinator = runtime["coordinator"]
    registry = er.async_get(hass)
    result: list[dict[str, Any]] = []
    for source in (coordinator.data or {}).get("cameras", []):
        camera = {
            key: source.get(key)
            for key in (
                "slug",
                "name",
                "id",
                "serial",
                "network_id",
                "camera_type",
                "product_type",
                "ptt_supported",
                "entity_id",
            )
        }
        slug = str(camera.get("slug") or "")
        live_state = next(
            (
                item
                for item in hass.states.async_all("camera")
                if item.attributes.get("proxy_slug") == slug
            ),
            None,
        )
        camera["live_entity_id"] = live_state.entity_id if live_state else ""

        source_entry = registry.async_get(str(camera.get("entity_id") or ""))
        device_id = source_entry.device_id if source_entry else None
        entries = (
            er.async_entries_for_device(
                registry, device_id, include_disabled_entities=True
            )
            if device_id
            else ([source_entry] if source_entry else [])
        )
        camera["entities"] = sorted(
            (_entity_summary(hass, item) for item in entries),
            key=lambda item: (item["domain"], item["name"]),
        )
        camera["capabilities"] = [
            "live_view",
            "local_clips",
            "snapshot_refresh",
            *(["push_to_talk"] if camera.get("ptt_supported") else []),
            *(
                ["motion_detection"]
                if any(
                    "motion" in item["entity_id"] for item in camera["entities"]
                )
                else []
            ),
        ]
        result.append(camera)
    return sorted(result, key=lambda item: item.get("name") or item.get("slug"))


def _blink_integration_facts(hass: HomeAssistant) -> dict[str, Any]:
    """What the official Blink integration is doing, if it is here at all.

    Three separate questions, because they fail separately: an entry can exist
    while sitting in setup_retry, and a loaded entry is still no use to
    snapshot refresh until blink.trigger_camera is actually registered.
    """
    from homeassistant.config_entries import ConfigEntryState

    entries = hass.config_entries.async_entries("blink")
    return {
        "blink_entries": len(entries),
        "blink_loaded": sum(
            1 for entry in entries if entry.state is ConfigEntryState.LOADED
        ),
        "blink_service": hass.services.has_service("blink", "trigger_camera"),
    }


async def _lovelace_resource_urls(hass: HomeAssistant) -> list[str] | None:
    """Every registered Lovelace resource URL, or None when it cannot be read.

    None is a real answer here and not an error: Lovelace may not have started,
    and a readout that reported "missing" in that window would send people to
    fix something that was already correct.
    """
    resources = resource_collection(hass.data.get("lovelace"))
    if resources is None:
        return None
    try:
        # Storage-backed resources are lazy; async_get_info loads them.
        await resources.async_get_info()
        return [str(item.get("url", "")) for item in resources.async_items() or []]
    except Exception:  # noqa: BLE001 - a readout must never break the panel
        LOGGER.debug("Could not read the Lovelace resource list", exc_info=True)
        return None


def _hacs_update_facts(hass: HomeAssistant) -> dict[str, Any]:
    """What HACS says about this integration's own version, if it tracks it.

    Matched on release_url, which HACS builds from the repository's full name.
    Its unique id is a GitHub repository id this code cannot know, and its
    name is renameable by the user, so neither identifies it reliably.
    """
    prefix = REPOSITORY_URL.lower()
    for state in hass.states.async_all("update"):
        url = str(state.attributes.get("release_url") or "").lower()
        if not url.startswith(prefix):
            continue
        return {
            "found": True,
            "update_available": state.state == "on",
            "installed": str(state.attributes.get("installed_version") or ""),
            "latest": str(state.attributes.get("latest_version") or ""),
        }
    return {"found": False}


async def _prerequisite_facts(
    hass: HomeAssistant,
    status: dict[str, Any],
    proxy_version: str | None,
    own_version: str,
) -> dict[str, Any]:
    """Collect everything prerequisites.build() decides from, and nothing else."""
    from homeassistant.const import __version__ as HA_VERSION

    return {
        "ha_version": HA_VERSION,
        "minimum_ha": MINIMUM_HA_VERSION,
        "required_blinkpy": REQUIRED_BLINKPY_VERSION,
        "environment_proxy_version": ENVIRONMENT_PROXY_VERSION,
        "proxy_version": proxy_version,
        "environment": status.get("environment"),
        "resource_url": FRONTEND_RESOURCE_URL,
        "legacy_resource_url": LEGACY_FRONTEND_RESOURCE_URL,
        "resource_urls": await _lovelace_resource_urls(hass),
        "lovelace_mode": resource_mode(hass.data.get("lovelace")),
        "integration_version": own_version,
        "hacs_update": _hacs_update_facts(hass),
        **_blink_integration_facts(hass),
    }


async def _panel_payload(hass: HomeAssistant) -> dict[str, Any]:
    """Build the admin panel's redacted, read-only snapshot.

    Answers before a config entry exists too. The panel is registered at
    integration setup so that it is there on the first restart after
    installing - exactly when someone most needs the install steps - and a
    503 at that moment was the one thing it showed. Without an entry every
    proxy-side check reads "not checked", the Home Assistant-side ones are
    real, and `configured` tells the panel which page to draw.
    """
    from homeassistant.loader import async_get_integration

    integration = await async_get_integration(hass, DOMAIN)
    own_version = str(integration.version or "")

    try:
        entry_id, runtime = _runtime_entry(hass)
    except web.HTTPServiceUnavailable:
        checks = prerequisites.build(
            await _prerequisite_facts(hass, {}, None, own_version)
        )
        return {
            "configured": False,
            "entry_id": None,
            "title": "Blink Live View Proxy",
            "base_url": "",
            "health": {},
            "status": {},
            "versions": {"integration": own_version, "proxy": "unknown", "behind": False},
            "update": {"available": False, "blocker": None, "method": None},
            "environment": {},
            "prerequisites": {
                "checks": checks,
                "summary": prerequisites.summarize(checks),
            },
            "cameras": [],
        }

    coordinator = runtime["coordinator"]
    data = coordinator.data or {}
    status = data.get("status") if isinstance(data.get("status"), dict) else {}
    proxy_version = infer_version(status)
    entry = hass.config_entries.async_get_entry(entry_id)
    checks = prerequisites.build(
        await _prerequisite_facts(hass, status, proxy_version, own_version)
    )
    return {
        "configured": True,
        "entry_id": entry_id,
        "title": entry.title if entry else "Blink Live View Proxy",
        "base_url": str(entry.data.get(CONF_BASE_URL, "")) if entry else "",
        "health": data.get("health") or {},
        "status": status,
        "versions": {
            "integration": own_version,
            "proxy": proxy_version or "unknown",
            "behind": is_behind(proxy_version, own_version),
        },
        "update": {
            "available": can_start_update(status),
            "blocker": update_blocker(status),
            "method": (status.get("update") or {}).get("method"),
        },
        "environment": status.get("environment") or {},
        "prerequisites": {
            "checks": checks,
            "summary": prerequisites.summarize(checks),
        },
        "cameras": _panel_cameras(hass, runtime),
    }


PANEL_UPDATE_MESSAGES = {
    "already_running": "An update is already running. Check again in a few minutes.",
    "entry_gone": "The integration entry is no longer loaded.",
    "no_addon": "Supervisor could not find the Blink Live View Proxy add-on.",
    "not_supported": "This proxy installation cannot update itself.",
    "update_failed": "The update could not be started. Check the Home Assistant and proxy logs.",
}


class BlinkLiveviewProxyPanelView(HomeAssistantView):
    """Return cameras, versions, health, and related native entities."""

    requires_auth = True
    url = "/api/blink_liveview_proxy/panel"
    name = "api:blink_liveview_proxy:panel"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @require_admin
    async def get(self, _request: web.Request) -> web.Response:
        return web.json_response(
            await _panel_payload(self.hass), headers=AUTH_VIEW_HEADERS
        )


class BlinkLiveviewProxyPanelUpdateView(HomeAssistantView):
    """Start the same guarded update offered by a Repairs Fix button."""

    requires_auth = True
    url = "/api/blink_liveview_proxy/panel/update"
    name = "api:blink_liveview_proxy:panel_update"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @require_admin
    async def post(self, _request: web.Request) -> web.Response:
        entry_id, _runtime_value = _runtime_entry(self.hass)
        try:
            await async_start_update(self.hass, entry_id)
        except UpdateAborted as err:
            status_code = {
                "already_running": 409,
                "not_supported": 501,
            }.get(err.reason, 503)
            return web.json_response(
                {
                    "started": False,
                    "reason": err.reason,
                    "message": PANEL_UPDATE_MESSAGES.get(
                        err.reason, PANEL_UPDATE_MESSAGES["update_failed"]
                    ),
                },
                status=status_code,
                headers=AUTH_VIEW_HEADERS,
            )
        return web.json_response(
            {
                "started": True,
                "message": "Update started. The proxy may be unavailable while it restarts.",
            },
            status=202,
            headers=AUTH_VIEW_HEADERS,
        )


class BlinkLiveviewProxyPanelYamlView(HomeAssistantView):
    """Render copy-ready dashboard YAML without exposing the proxy token."""

    requires_auth = True
    url = "/api/blink_liveview_proxy/panel/yaml"
    name = "api:blink_liveview_proxy:panel_yaml"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    @require_admin
    async def get(self, request: web.Request) -> web.Response:
        output_format = request.query.get("format", "dashboard")
        if output_format not in {"dashboard", "view", "card"}:
            raise web.HTTPBadRequest(text="Unknown YAML format\n")
        cameras = (await _panel_payload(self.hass))["cameras"]
        camera_slug = request.query.get("camera", "")
        if camera_slug:
            cameras = [item for item in cameras if item.get("slug") == camera_slug]
            if not cameras:
                raise web.HTTPNotFound(text="Unknown camera slug\n")
        return web.json_response(
            {"yaml": render_dashboard_yaml(cameras, output_format)},
            headers=AUTH_VIEW_HEADERS,
        )
