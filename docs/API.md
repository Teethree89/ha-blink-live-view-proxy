# Proxy API

These are the local proxy endpoints used by the Home Assistant custom
integration and direct player. Keep the proxy bound to `127.0.0.1` unless you
also configure `BLINK_PROXY_TOKEN`.

The route handlers live in
`proxy/blink_proxy/routes.py`; protocol-specific push-to-talk handling lives in
`proxy/blink_proxy/ptt.py`. That split is intentional so Blink endpoint changes
can usually be patched without touching CLI, install, or HACS packaging code.

## Health and Inventory

- `GET /health`
- `GET /status`
- `GET /cameras`
- `GET /`

`GET /status` reports login readiness, the `auth_state` of the login state
machine, discovered vs configured camera counts, the cached Blink token expiry,
process uptime, and the watchdog's last restart and attempt count. It is
unauthenticated like `/health`, so it deliberately exposes no camera names,
serials, tokens, usernames, or challenge identifiers.

`version` and `update` are the exceptions: when a token is configured, they are
included only for an authorized request, because a version number and install
method are a shopping list for whoever is asking. A tokenless proxy keeps the
legacy public `version`, but omits `update` because `/update` must refuse that
configuration. The integration's version check sends the token when it has
one. `update` names one of `systemd`, `supervisor`, `container`, or `manual`,
says whether the proxy can start an update itself, and includes a human-readable
reason when it cannot.

A negative `token_seconds_remaining` is normal and is not a fault. BlinkPy
refreshes lazily: `Auth.query()` checks `need_refresh()` and renews the token
inline before the request that needs it. An idle proxy therefore sits with an
expired token until the next live view, then refreshes on demand and persists
the result through the auth-file callback.

Treat `ready` and `cameras_discovered` as the liveness signals. What matters
is not whether the token has expired but whether a *refresh* can still
succeed — a different question, and the one worth alerting on.

## Self-update

- `POST /update`

This route exists so the integration's repair flow can start the updater on a
systemd proxy host. It requires the proxy token in an `Authorization: Bearer`
header; `?token=` is deliberately rejected. The request body is ignored and
cannot select a tag, branch, or repository. The updater installed on the host
always follows the newest release tag configured there.

The proxy answers `202` once
`blink-liveview-proxy-update.service` has been started with `--no-block`. That
separate systemd unit survives the proxy restart the upgrade causes. `409`
means the updater is already running, `501` means this install cannot update
itself, and `503` means no proxy token is configured. Add-on updates do not use
this route: Home Assistant asks Supervisor directly. Standalone containers
must be pulled and recreated by their owner.

## Authentication Control

These routes drive browser login and deliberate reauthentication. They are the
only routes that carry credentials, and they are held to stricter rules than the
media routes.

- `GET /auth/status`
- `POST /auth/login` — body `{"username": "...", "password": "..."}`
- `POST /auth/pin` — body `{"challenge_id": "...", "pin": "123456"}`
- `POST /auth/cancel` — body `{"challenge_id": "..."}`

Access control:

- A proxy token must be configured. Without one every route here answers
  `503`; browser authentication is never an unauthenticated LAN endpoint.
- Authorization is accepted **only** as an `Authorization: Bearer` header.
  Unlike the media routes, the `?token=` query form is rejected, because URLs
  end up in history, logs, referrers, and caches.
- Bodies are capped at 4 KB and must be JSON objects.
- Every response is `Cache-Control: no-store` with `Referrer-Policy: no-referrer`
  and `X-Content-Type-Options: nosniff`.

All four return the same public status object, and nothing else:

```json
{
  "state": "waiting_for_pin",
  "message": "Blink sent a new PIN. Enter that PIN without restarting the proxy.",
  "authenticated": true,
  "challenge_id": "opaque-correlation-id",
  "expires_in": 540,
  "can_submit_pin": true,
  "can_start": false,
  "can_cancel": true
}
```

`state` is one of `idle`, `authenticating`, `waiting_for_pin`, `success`,
`expired`, `failure`. The username, password, PIN, refresh token, and upstream
error text never appear in it; failures are reported as classified, redacted
messages. `challenge_id` is a random correlation id for the live challenge only
— it is not a credential and is `null` once the attempt ends.

Status codes:

| Code | Meaning |
|---|---|
| `200` | Status or cancellation accepted |
| `202` | Login or PIN accepted for processing; poll `GET /auth/status` |
| `400` | Missing/invalid credentials, a non-numeric PIN, or an unparseable body |
| `401` | Missing or wrong bearer token |
| `409` | A login is already active, or the challenge is stale |
| `503` | No proxy token is configured |

Exactly one challenge exists at a time. A PIN is only accepted for the challenge
that requested it, the previously working Blink client keeps serving until a
candidate login has succeeded and committed its auth cache, and an unanswered
challenge expires after fifteen minutes. Restarting the service drops the in-memory
challenge — a new login and a newly issued PIN are required afterwards.

### Home Assistant side

The custom integration exposes the same three actions to its admin-only panel,
so the browser never holds the proxy token:

- `GET /api/blink_liveview_proxy/auth/status`
- `POST /api/blink_liveview_proxy/auth/login`
- `POST /api/blink_liveview_proxy/auth/pin`
- `POST /api/blink_liveview_proxy/auth/cancel`

They require an authenticated Home Assistant **administrator** (`requires_auth`
plus `require_admin`), forward only the JSON body, add the bearer token
server-side, and return the proxy's redacted status object. Upstream failures
are collapsed into a generic `502` so proxy URLs and library error text are not
reflected back to the browser.

### Home Assistant admin dashboard

The tabbed **Blink Live View Proxy** sidebar panel uses three additional Home Assistant
routes. They require an authenticated administrator and never return the proxy
API token or Blink credentials:

- `GET /api/blink_liveview_proxy/panel` — health, versions, update capability,
  discovered cameras, and related Home Assistant entity summaries
- `POST /api/blink_liveview_proxy/panel/update` — starts the same systemd or
  Supervisor update used by a Repairs Fix button; the body chooses nothing
- `GET /api/blink_liveview_proxy/panel/yaml?format=dashboard|view|card` — emits
  copy-ready Lovelace YAML; optional `camera=slug` limits it to one camera

Entity summaries contain IDs, display names, current string states, units, and
device classes so the panel can link to Home Assistant's native More Info
dialog. They do not duplicate service calls or expose arbitrary attributes.

## Live View

- `GET /cameras/{slug}/mpegts`
- `GET /cameras/{slug}/hls/index.m3u8`
- `GET /cameras/{slug}/hls/{filename}`

Useful `mpegts` query parameters:

- `seconds`: maximum session length requested by the direct player
- `session`: browser session ID used by push-to-talk
- `force=1`: bypass local cooldown after a previous live view

## Push-to-Talk

- `GET /cameras/{slug}/ptt`

This is a WebSocket endpoint. The browser sends start/stop JSON messages and
binary signed 16-bit PCM chunks. The proxy encodes AAC with `ffmpeg` and sends
Blink IMMI audio frames over the active live-view session.

## Last Watched Live View

- `GET /cameras/{slug}/last-liveview`
- `GET /cameras/{slug}/last-liveview.ts`
- `GET /cameras/{slug}/last-liveview.mp4`

The MP4 endpoint remuxes the cached MPEG-TS file with `ffmpeg` on demand.

## Clips

- `GET /clips?source=both&hours=24&limit=20`
- `GET /clips/{clip_id}.mp4?source=local`
- `GET /clips/{clip_id}.jpg?source=cloud`

`source` is `local`, `cloud`, or — on the listing only — `both`, which is what
the Home Assistant viewer asks for by default. The download and thumbnail
routes name one inventory, never both, and a clip id that is not a
24-character hex digest is refused before it reaches the cache or Blink.

Listing either inventory is metadata only. Fetching is not, and that is the
difference worth knowing: a local clip comes off your own Sync Module, while a
cloud clip is downloaded from Blink, which is why the viewer draws cloud
thumbnails only for the newest few and leaves the rest until asked.

Each listed clip carries a `download_url`, a `thumbnail_url` and `cached`.
The first request for a clip fetches it from Blink and keeps it under
`clip_cache_dir`; the thumbnail is its first frame, cut with ffmpeg from that
copy. Both are then served as files, so they answer byte ranges — which is
what lets the viewer seek, and what Safari needs before it plays an MP4 at
all — and a browser may keep them for a day (`Cache-Control: private`). The
id is a hash of the clip's identity, so the bytes behind it never change.

Fetches from Blink run one at a time. blinkpy's `prepare_download()` has the
Sync Module upload the clip to the cloud and polls until it lands, and several
of those at once is the pattern that gets an account rate-limited. A listing
also remembers its clips by id for fifteen minutes, so a thumbnail or download
does not refresh the module's manifest just to find the clip again.

The cache is pruned oldest-first past `clip_cache_max_mb` (default 512). A
thumbnail is a few kilobytes beside its clip, so the cap really bounds how many
clips stay instantly replayable.
