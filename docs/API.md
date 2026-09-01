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

`version` is the exception: it is included only when the request carries the
proxy token, because a version number is a shopping list for whoever is asking.
The integration's version check sends the token, so it sees it; anything else on
the LAN gets the rest of the payload unchanged.

A negative `token_seconds_remaining` is normal and is not a fault. BlinkPy
refreshes lazily: `Auth.query()` checks `need_refresh()` and renews the token
inline before the request that needs it. An idle proxy therefore sits with an
expired token until the next live view, then refreshes on demand and persists
the result through the auth-file callback.

Treat `ready` and `cameras_discovered` as the liveness signals. What matters
is not whether the token has expired but whether a *refresh* can still
succeed — a different question, and the one worth alerting on.

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

## Local Clips

- `GET /clips?source=local&hours=24&limit=20`
- `GET /clips/{clip_id}.mp4?source=local`

The Home Assistant viewer intentionally uses local Sync Module clips. Cloud clip
support remains a diagnostic proxy path and is not surfaced in the HA viewer.
