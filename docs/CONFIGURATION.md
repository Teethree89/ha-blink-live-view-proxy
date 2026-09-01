# Configuration

## Proxy Config

The proxy reads JSON from:

1. `--config /path/to/config.json`
2. `BLINK_PROXY_CONFIG`
3. `config.json` next to `blink_liveview_proxy.py`

Important fields:

```json
{
  "host": "127.0.0.1",
  "port": 8088,
  "auth_file": "/var/lib/blink-liveview-proxy/secrets/blink-auth.json",
  "ffmpeg": "ffmpeg",
  "liveview_cache_dir": "/var/lib/blink-liveview-proxy/liveviews",
  "mpegts_session_seconds": 60,
  "mpegts_cooldown_seconds": 30,
  "ptt_force_enabled_slugs": [],
  "ptt_disabled_camera_types": ["mini"],
  "ptt_disabled_product_types": ["owl"],
  "cameras": {}
}
```

## Camera Map

The proxy can discover Blink cameras without a camera map. A map is still useful
for stable slugs and for linking proxy cameras back to official Home Assistant
Blink camera entities.

Use `name`, `id`, or `serial` to match a Blink camera:

```json
{
  "cameras": {
    "front_door": {
      "name": "Front Door",
      "entity_id": "camera.front_door"
    }
  }
}
```

`entity_id` should point to the official HA Blink camera entity. That lets the
custom integration use the normal snapshot in loading screens and snapshot
refresh actions.

## Live View Duration

The Home Assistant integration has an options flow:

```text
Settings > Devices & services > Blink Liveview Proxy > Configure
```

`Live-view duration in seconds` controls how long the direct player asks the HA
route/proxy to keep each live view open. Valid range: `10-300` seconds.

Blink can still end sessions early.

### `send_liveview_token`

Stock BlinkPy sends 64 null bytes in the auth-token field of the IMMI
handshake (see `blinkpy/livestream.py`, `get_auth_header()` — the code comment
literally says "64 null bytes for now"). `TokenAwareBlinkLiveStream` fixes
this by populating that field with the real `liveview_token` from the
liveview response.

Informal testing on a Blink Outdoor 4 (2K+, Sync Module 2, IMMI liveview)
found that individual segments consistently lasted longer with the real
token (roughly 3-8s per segment across a 100+ second run) than with the
stock zero-byte handshake (a very consistent ~2-2.5s cutoff across repeated
runs). This isn't a rigorous benchmark and Blink can still end sessions
early either way, but the pattern was consistent enough that `true` is now
the default. Set it back to `false` if you hit issues and want to compare.

## Push-to-Talk

Browser microphone capture requires HTTPS or a browser-trusted origin. The
player sends PCM to HA over WebSocket; HA forwards it to the proxy; the proxy
uses ffmpeg to encode AAC and sends IMMI audio frames to Blink.

PTT is hidden for camera families in:

```json
"ptt_force_enabled_slugs": [],
"ptt_disabled_camera_types": ["mini"],
"ptt_disabled_product_types": ["owl"]
```

Add a slug such as `"kitchen"` to `ptt_force_enabled_slugs` only for targeted
testing of cameras that are disabled by family defaults. A Blink Mini/`owl`
camera was confirmed audible this way on June 30, 2026.

## Local Clips

The HA clip viewer intentionally uses local Sync Module clips:

```text
/api/blink_liveview_proxy/clips/viewer
```

The proxy also has diagnostic support for cloud clips, but the HA viewer does
not expose them.

## Proxy Token

Both install paths provision this, so there is normally nothing to do:

| Install | Where the token comes from | Where it lives |
|---|---|---|
| `scripts/install-proxy.sh` | Generated on first install, never rotated after | `/etc/blink-liveview-proxy/blink-liveview-proxy.env` |
| Add-on | Generated on first start unless `proxy_api_token` is set | `/data/proxy-token`, shared with the integration |

To set one yourself on a manual install, the service reads it from the
environment:

```bash
export BLINK_PROXY_TOKEN="long-random-token"
```

Enter the same token in the HA integration config flow — the add-on pre-fills
it. If a token is ever rejected, Home Assistant opens a reauthentication prompt
for that entry rather than failing silently.

The token is also what gates browser authentication. `/auth/*` answers `503`
while it is empty, and accepts it only as an `Authorization: Bearer` header —
never as `?token=`. Keep the token out of dashboards, shell history, and URLs;
Home Assistant adds it server-side for the authentication panel, so no browser
ever receives it.

## Blink Account Authentication

Blink credentials belong to the proxy, not the integration. The integration
configures only the proxy URL, live-view duration, and this proxy token.

Three ways to authenticate, all documented step by step in
[OPERATIONS.md](OPERATIONS.md#re-authenticating):

| Where | How the PIN is delivered |
|---|---|
| **Blink Authentication** panel | Typed into the page that started the login |
| Add-on without a browser | `blink_2fa_code` option, saved while it runs |
| Linux CLI | Answered at the interactive `Blink 2FA code:` prompt |

In all three the PIN is only issued *after* sign-in begins and is only valid for
the process that asked for it, so a start-time value in `--pin` or
`BLINK_2FA_CODE` (the `twofa_env` field) is deliberately ignored with a warning.
The `auth_file` holds the resulting refresh data; back it up like a secret and
keep it out of git.
