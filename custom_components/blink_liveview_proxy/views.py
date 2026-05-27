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
from typing import Any
from urllib.parse import quote, urlencode

from aiohttp import ClientError, ClientResponse, ClientTimeout, WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import KEY_AUTHENTICATED

from .api import BlinkLiveviewProxyClient
from .const import DEFAULT_STREAM_SECONDS, DOMAIN

LOGGER = logging.getLogger(__name__)

STATIC_ROOT = Path(__file__).parent / "frontend"
PLAYER_LIBRARY_URL = "/api/blink_liveview_proxy/static/mpegts.min.js"
BROWSER_TOKEN_TTL_SECONDS = 10 * 60
BROWSER_TOKEN_MAX_COUNT = 128


def async_register_views(hass: HomeAssistant) -> None:
    """Register browser-facing proxy views."""
    if hass.data.setdefault(DOMAIN, {}).get("_views_registered"):
        return

    hass.http.register_view(BlinkLiveviewProxyStaticView(hass))
    hass.http.register_view(BlinkLiveviewProxyPlayerView(hass))
    hass.http.register_view(BlinkLiveviewProxyMpegtsView(hass))
    hass.http.register_view(BlinkLiveviewProxyPttView(hass))
    hass.http.register_view(BlinkLiveviewProxyLastLiveviewInfoView(hass))
    hass.http.register_view(BlinkLiveviewProxyLastLiveviewDownloadView(hass))
    hass.http.register_view(BlinkLiveviewProxyLastLiveviewMp4DownloadView(hass))
    hass.http.register_view(BlinkLiveviewProxySnapshotRefreshView(hass))
    hass.http.register_view(BlinkLiveviewProxyClipsView(hass))
    hass.http.register_view(BlinkLiveviewProxyClipDownloadView(hass))
    hass.http.register_view(BlinkLiveviewProxyClipsViewerView(hass))
    hass.data[DOMAIN]["_views_registered"] = True


class BlinkLiveviewProxyStaticView(HomeAssistantView):
    """Serve package frontend assets used by dashboards and the player."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/static/{filename}"
    name = "api:blink_liveview_proxy:static"

    _content_types = {
        "blink-liveview-dialog.js": "application/javascript",
        "mpegts.min.js": "application/javascript",
    }

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, _request: web.Request, filename: str) -> web.FileResponse:
        """Return one bundled JavaScript asset."""
        if filename not in self._content_types:
            raise web.HTTPNotFound()

        path = STATIC_ROOT / filename
        if not path.exists():
            raise web.HTTPNotFound(text=f"Missing static asset: {filename}\n")

        return web.FileResponse(
            path,
            headers={
                "Cache-Control": "no-cache",
                "Content-Type": self._content_types[filename],
            },
        )


def _runtime(hass: HomeAssistant) -> dict[str, Any]:
    """Return the first configured integration runtime."""
    for key, value in hass.data.get(DOMAIN, {}).items():
        if not str(key).startswith("_") and isinstance(value, dict):
            return value
    raise web.HTTPServiceUnavailable(text="Blink live-view proxy is not configured\n")


def _client(hass: HomeAssistant) -> BlinkLiveviewProxyClient:
    return _runtime(hass)["client"]


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
) -> ClientResponse:
    """Open a streaming response from the local proxy."""
    try:
        response = await client._session.get(  # noqa: SLF001
            client.proxy_url(path, query),
            headers=client.auth_headers(),
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
) -> web.StreamResponse:
    """Stream bytes from the local proxy to the browser."""
    upstream = await _open_proxy_response(_client(hass), path, query)
    headers = {
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    }
    if download_filename:
        headers["Content-Disposition"] = (
            f'attachment; filename="{download_filename}"'
        )
    else:
        upstream_disposition = upstream.headers.get("Content-Disposition")
        if upstream_disposition:
            headers["Content-Disposition"] = upstream_disposition
    response = web.StreamResponse(
        status=200,
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
  top:16px;
  right:16px;
  z-index:4;
  display:flex;
  gap:10px;
}}
.live-actions[hidden] {{
  display:none;
}}
button,a.button {{
  appearance:none;
  border:0;
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
    <button id="talk" class="talk" type="button" disabled>Hold Talk</button>
    <button id="endSave" class="danger" type="button">End &amp; Save</button>
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
const endSave = document.getElementById("endSave");
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

function streamUrl() {{
  const token = encodeURIComponent(accessToken || "");
  const session = encodeURIComponent(sessionId);
  const path = `/api/blink_liveview_proxy/cameras/${{slug}}/mpegts?token=${{token}}&seconds=${{seconds}}&force=1&session=${{session}}&cache=${{Date.now()}}`;
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

function lastLiveviewInfoUrl() {{
  const token = encodeURIComponent(accessToken || "");
  const path = `/api/blink_liveview_proxy/cameras/${{slug}}/last-liveview?token=${{token}}&cache=${{Date.now()}}`;
  return new URL(path, window.location.origin).href;
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

function liveviewKey(info) {{
  if (!info || !info.available) {{
    return "";
  }}
  return `${{info.filename || ""}}:${{info.bytes || ""}}:${{info.ended_at || ""}}`;
}}

async function currentLiveviewInfo() {{
  const response = await fetch(lastLiveviewInfoUrl(), {{
    cache: "no-store",
    credentials: "same-origin"
  }});
  if (!response.ok) {{
    return null;
  }}
  return response.json();
}}

async function waitForFinalizedLiveview(previousKey) {{
  for (let attempt = 0; attempt < 10; attempt += 1) {{
    await delay(attempt === 0 ? 800 : 700);
    const info = await currentLiveviewInfo();
    const key = liveviewKey(info);
    if (key && key !== previousKey) {{
      return info;
    }}
  }}
  throw new Error("No newly finalized live view was found");
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

async function endAndSaveCurrentStream() {{
  const originalText = endSave.textContent;
  endSave.disabled = true;
  endSave.textContent = "Saving";

  try {{
    const previousKey = liveviewKey(await currentLiveviewInfo().catch(() => null));
    stopPlayer();
    setLoading("Saving current live view");
    await waitForFinalizedLiveview(previousKey);
    const response = await fetchLastViewMp4(4);
    await downloadMp4(response);
    setEnded("Live view saved.");
  }} catch (err) {{
    setEnded("Could not save the current live view.");
  }} finally {{
    endSave.disabled = false;
    endSave.textContent = originalText;
  }}
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

  if (!window.mpegts || !mpegts.getFeatureList().mseLivePlayback) {{
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
    stopPlayer();
    setEnded("Live view ended or the camera stopped sending video.");
  }});

  video.onplaying = () => {{
    video.classList.add("ready");
    overlay.classList.add("hidden");
    actions.hidden = true;
    liveActions.hidden = false;
    talk.hidden = !pttSupported;
    talk.disabled = !pttSupported;
  }};

  video.onended = () => {{
    stopPlayer();
    setEnded("Live view ended.");
  }};

  player.attachMediaElement(video);
  player.load();

  try {{
    await video.play();
  }} catch (err) {{
    statusText.textContent = "Tap play to start live view";
  }}

  endTimer = setTimeout(() => {{
    stopPlayer();
    setEnded(`${{seconds}} second live view finished.`);
  }}, (seconds + 5) * 1000);
}}

restart.addEventListener("click", startPlayer);
save.addEventListener("click", saveLastView);
endSave.addEventListener("click", endAndSaveCurrentStream);
talk.addEventListener("pointerdown", startTalk);
talk.addEventListener("pointerup", stopTalk);
talk.addEventListener("pointercancel", stopTalk);
talk.addEventListener("pointerleave", stopTalk);
window.addEventListener("blur", stopTalk);
window.addEventListener("beforeunload", () => {{
  stopTalk();
}});
talk.hidden = !pttSupported;
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

        await self.hass.services.async_call(
            "blink",
            "trigger_camera",
            {"entity_id": source_entity_id},
            blocking=True,
        )
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
    """Rewrite proxy-relative clip URLs into authenticated HA API URLs."""
    for clip in payload.get("clips", []):
        if not isinstance(clip, dict):
            continue
        download_url = str(clip.get("download_url") or "")
        if download_url.startswith("/clips/"):
            clip["download_url"] = f"/api/blink_liveview_proxy{download_url}"
        if access_token and clip.get("download_url"):
            separator = "&" if "?" in str(clip["download_url"]) else "?"
            clip["download_url"] = (
                f"{clip['download_url']}{separator}token="
                f"{quote(access_token, safe='')}"
            )
    return payload


def _clips_viewer_html(camera_slug: str | None, access_token: str) -> str:
    """Return the local Sync Module clip viewer page."""
    camera_json = json.dumps(camera_slug or "")
    token_json = json.dumps(access_token)
    html_text = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Blink Local Clips</title>
<style>
html,body {
  margin:0;
  min-height:100%;
  background:#05070a;
  color:#f8fafc;
  font-family:Arial,Helvetica,sans-serif;
}
body {
  display:grid;
  grid-template-rows:auto auto 1fr;
}
header {
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
  min-height:56px;
  padding:0 16px;
  background:#111827;
  border-bottom:1px solid rgba(148,163,184,.2);
}
h1 {
  margin:0;
  font-size:18px;
  line-height:1.2;
}
.controls {
  display:flex;
  flex-wrap:wrap;
  gap:10px;
  align-items:center;
  padding:12px 16px;
  background:#0b1018;
  border-bottom:1px solid rgba(148,163,184,.16);
}
label {
  display:grid;
  gap:4px;
  color:#cbd5e1;
  font-size:12px;
  font-weight:700;
}
select,button {
  min-height:36px;
  border:1px solid rgba(148,163,184,.28);
  border-radius:6px;
  background:#111827;
  color:#f8fafc;
  font:inherit;
}
select {
  min-width:138px;
  padding:0 10px;
}
button,a.button {
  display:inline-grid;
  place-items:center;
  min-width:82px;
  padding:0 12px;
  text-decoration:none;
  font-weight:800;
  cursor:pointer;
}
button.primary {
  border-color:#0284c7;
  background:#0284c7;
}
main {
  display:grid;
  grid-template-columns:minmax(280px,420px) 1fr;
  min-height:0;
}
.list {
  overflow:auto;
  border-right:1px solid rgba(148,163,184,.16);
}
.empty,.loading {
  padding:28px 18px;
  color:#cbd5e1;
}
.clip {
  display:grid;
  gap:8px;
  padding:14px 16px;
  border-bottom:1px solid rgba(148,163,184,.14);
}
.clip strong {
  font-size:15px;
}
.meta {
  color:#cbd5e1;
  font-size:13px;
}
.row-actions {
  display:flex;
  flex-wrap:wrap;
  gap:8px;
}
.preview {
  display:grid;
  grid-template-rows:1fr auto;
  min-width:0;
  min-height:0;
  background:#020617;
}
video {
  width:100%;
  height:100%;
  min-height:260px;
  object-fit:contain;
  background:#020617;
}
.preview-title {
  padding:12px 16px;
  color:#cbd5e1;
  background:#0b1018;
  border-top:1px solid rgba(148,163,184,.16);
}
@media (max-width: 780px) {
  main {
    grid-template-columns:1fr;
  }
  .list {
    max-height:42vh;
    border-right:0;
    border-bottom:1px solid rgba(148,163,184,.16);
  }
}
</style>
</head>
<body>
<header>
  <h1>Blink Local Clips</h1>
  <span id="summary" class="meta"></span>
</header>
<section class="controls">
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
  <label>Limit
    <select id="limit">
      <option value="30">30 clips</option>
      <option value="60" selected>60 clips</option>
      <option value="100">100 clips</option>
    </select>
  </label>
  <button id="refresh" class="primary" type="button">Refresh</button>
</section>
<main>
  <section id="list" class="list">
    <div class="loading">Loading local Sync Module clips...</div>
  </section>
  <section class="preview">
    <video id="video" controls playsinline></video>
    <div id="previewTitle" class="preview-title">Select a clip to preview it here.</div>
  </section>
</main>
<script>
const list = document.getElementById("list");
const video = document.getElementById("video");
const summary = document.getElementById("summary");
const previewTitle = document.getElementById("previewTitle");
const hours = document.getElementById("hours");
const limit = document.getElementById("limit");
const camera = document.getElementById("camera");
const refresh = document.getElementById("refresh");
const initial = new URLSearchParams(window.location.search);
const fixedCamera = __CAMERA_JSON__;
const accessToken = __TOKEN_JSON__;
let clips = [];

if (fixedCamera || initial.get("camera")) {
  const slug = fixedCamera || initial.get("camera");
  camera.append(new Option(slug, slug));
  camera.value = slug;
  camera.disabled = Boolean(fixedCamera);
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short"
  }).format(date);
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

function updateCameraOptions() {
  const selected = camera.value;
  const seen = new Map();
  for (const clip of clips) {
    if (!clip.slug) continue;
    seen.set(clip.slug, optionLabel(clip));
  }
  camera.replaceChildren(new Option("All cameras", ""));
  for (const [slug, label] of [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]))) {
    camera.append(new Option(label, slug));
  }
  if (selected && seen.has(selected)) {
    camera.value = selected;
  }
}

function render() {
  const selected = fixedCamera || camera.value;
  const visible = selected ? clips.filter((clip) => clip.slug === selected) : clips;
  summary.textContent = visible.length ? `${visible.length} local clip${visible.length === 1 ? "" : "s"}` : "";
  list.replaceChildren();
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No local Sync Module clips found for this window.";
    list.append(empty);
    return;
  }
  for (const clip of visible) {
    const row = document.createElement("article");
    row.className = "clip";
    const title = document.createElement("strong");
    title.textContent = optionLabel(clip);
    const meta = document.createElement("div");
    meta.className = "meta";
    const size = formatSize(clip.size);
    meta.textContent = `${formatTime(clip.created_at)}${size ? ` - ${size}` : ""}`;
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const preview = document.createElement("button");
    preview.type = "button";
    preview.textContent = "Preview";
    preview.addEventListener("click", () => {
      video.src = clip.download_url;
      video.load();
      video.play().catch(() => {});
      previewTitle.textContent = `${optionLabel(clip)} - ${formatTime(clip.created_at)}`;
    });
    const download = document.createElement("a");
    download.className = "button";
    download.href = clip.download_url;
    download.textContent = "Download";
    actions.append(preview, download);
    row.append(title, meta, actions);
    list.append(row);
  }
}

async function loadClips() {
  refresh.disabled = true;
  list.innerHTML = '<div class="loading">Loading local Sync Module clips...</div>';
  const params = new URLSearchParams({
    hours: hours.value,
    limit: limit.value
  });
  if (fixedCamera || camera.value) {
    params.set("camera", fixedCamera || camera.value);
  }
  if (accessToken) {
    params.set("token", accessToken);
  }
  const response = await fetch(`/api/blink_liveview_proxy/clips?${params}`, {
    cache: "no-store",
    credentials: "same-origin"
  });
  if (!response.ok) {
    list.innerHTML = '<div class="empty">Could not load local clips.</div>';
    summary.textContent = "";
    refresh.disabled = false;
    return;
  }
  const data = await response.json();
  clips = Array.isArray(data.clips) ? data.clips : [];
  updateCameraOptions();
  render();
  refresh.disabled = false;
}

refresh.addEventListener("click", loadClips);
hours.addEventListener("change", loadClips);
limit.addEventListener("change", loadClips);
camera.addEventListener("change", render);
loadClips();
</script>
</body>
</html>"""
    return (
        html_text.replace("__CAMERA_JSON__", camera_json)
        .replace("__TOKEN_JSON__", token_json)
    )


class BlinkLiveviewProxyClipsView(HomeAssistantView):
    """Proxy recent Blink clip metadata."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/clips"
    name = "api:blink_liveview_proxy:clips"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Return recent local Sync Module clip metadata from the local proxy."""
        camera_slug = request.query.get("camera") or None
        if camera_slug:
            _camera(self.hass, camera_slug)
            _authorize_browser_request(self.hass, request, camera_slug)
        elif not request.get(KEY_AUTHENTICATED, False):
            raise web.HTTPForbidden(text="Missing camera token\n")

        allowed = {"camera", "hours", "pages", "limit"}
        query = {
            key: value
            for key, value in request.query.items()
            if key in allowed
        }
        query["source"] = "local"
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
    """Proxy one local Sync Module clip download."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/clips/{clip_id}.mp4"
    name = "api:blink_liveview_proxy:clip_download"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, clip_id: str) -> web.StreamResponse:
        """Download one local Sync Module clip."""
        camera_slug = request.query.get("camera") or None
        if camera_slug:
            _camera(self.hass, camera_slug)
            _authorize_browser_request(self.hass, request, camera_slug)
        elif not request.get(KEY_AUTHENTICATED, False):
            raise web.HTTPForbidden(text="Missing camera token\n")

        allowed = {"camera", "hours", "pages", "limit"}
        query = {
            key: value
            for key, value in request.query.items()
            if key in allowed
        }
        query["source"] = "local"
        return await _proxy_stream(
            self.hass,
            request,
            f"/clips/{clip_id}.mp4",
            "video/mp4",
            query,
        )


class BlinkLiveviewProxyClipsViewerView(HomeAssistantView):
    """Serve the local Sync Module clips viewer."""

    requires_auth = False
    url = "/api/blink_liveview_proxy/clips/viewer"
    name = "api:blink_liveview_proxy:clips_viewer"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Return the local clips viewer HTML."""
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
            text=_clips_viewer_html(camera_slug, access_token),
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )
