# Blink Live View Proxy Custom Integration

Unofficial local Home Assistant wrapper for the Blink Live View Proxy service.

This integration does not log in to Blink and does not store Blink credentials.
It only talks to the local proxy HTTP API:

- `GET /health`
- `GET /cameras`
- `GET /cameras/{slug}/mpegts`
- `GET /clips?source=local`
- `GET /clips/{clip_id}.mp4?source=local`
- `GET /clips/{clip_id}.jpg?source=local` — the first frame, from proxy 0.7.0
- `GET /auth/status`, `POST /auth/login`, `POST /auth/pin`, `POST /auth/cancel`
- authenticated `GET /status` version/update capability and `POST /update`

The proxy remains responsible for Blink OAuth, two-factor login, token refresh,
the `immis://` bridge, and HLS generation. The `/auth/*` routes are only
forwarded on behalf of a signed-in Home Assistant administrator: the credentials
and PIN pass through in request bodies, are never persisted by Home Assistant,
and the refresh token stays inside the proxy's `auth_file`.

The publishable package lives in the
[Blink Live View Proxy repository](https://github.com/Teethree89/ha-blink-live-view-proxy).

This integration is not affiliated with, endorsed by, or supported by Amazon or
Blink. It is an interoperability layer for cameras you own.

## Requirements

| What | Why |
|---|---|
| Home Assistant **2024.11.0+** | Enforced by `hacs.json`; the panel and repair notices rely on it |
| A running proxy, **0.3.0 or newer** | This integration is only a client. Older proxies work for live view but have no `/auth` routes, and the integration says so rather than failing quietly |
| A proxy API token | Required for **Blink Proxy → Authentication**, and for any proxy not bound to loopback. Both installers generate one |
| Matching proxy and integration releases | A newer integration raises a repair issue and offers **Fix** when systemd or Supervisor can perform the update |
| `button-card` (HACS → Frontend) | Every example dashboard fires `fire-dom-event` through it |
| `auto-entities` (HACS → Frontend) | Only for the self-populating dashboard |
| The official Blink integration | Optional. Snapshots behind the loading frame, the snapshot-refresh button, and motion switches come from it |

This integration installs no Python dependencies of its own — it uses Home
Assistant's `aiohttp` — and depends only on the built-in `http`, `frontend` and
`panel_custom` components.

## Local Test Run

From the repo root:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r proxy/requirements.txt
python proxy/blink_liveview_proxy.py --config /path/to/config.json serve
```

If `BLINK_PROXY_TOKEN` is set for the proxy, enter the same token in the
integration setup form.

For first-time Blink auth, run the proxy CLI once before starting `serve`:

```bash
BLINK_USERNAME='you@example.com' python proxy/blink_liveview_proxy.py \
  --config /path/to/config.json list
```

The CLI submits the credentials first and prompts `Blink 2FA code:` only after
Blink has issued a PIN. Enter that new PIN in the same session — do not restart
between the two steps, and do not pre-set `BLINK_2FA_CODE`, which is ignored
because it can only be from an earlier attempt. The proxy stores the Blink
refresh token in `secrets/blink-auth.json`; this integration only sees the local
proxy URL.

Once a proxy token is configured on both sides, later logins can be done from
the browser instead — see **Blink Live View Proxy panel** below.

## Home Assistant Setup

With HACS, add
[Blink Live View Proxy](https://github.com/Teethree89/ha-blink-live-view-proxy)
as a custom repository of type `Integration`, download it, and restart Home
Assistant.

After the custom component is present under Home Assistant's
`custom_components/` directory and Home Assistant has restarted:

1. Go to Settings > Devices & services.
2. Add integration: `Blink Live View Proxy`.
3. Use `http://127.0.0.1:8088` when the proxy runs on the HA host.
4. Use `http://<mac-lan-ip>:8088` when testing against a proxy running on the
   Mac with `--host 0.0.0.0`.
5. Set the live-view duration. The default is 60 seconds; valid values are
   10-300 seconds.

The integration creates:

- an admin-only, tabbed **Blink Live View Proxy** panel at `/blink-liveview-proxy-auth`
  with health/version details, camera capabilities, links to each related
  native Home Assistant entity, browser authentication, and YAML export
- one `camera.blink_live_*` stream entity per proxy camera
- authenticated direct browser player URLs at
  `/api/blink_liveview_proxy/cameras/{slug}/player`
- a token refresh route at `/api/blink_liveview_proxy/cameras/{slug}/token`,
  which mints a fresh short-lived browser token for one camera. A player left
  open outlives its token — Home Assistant rotates camera access tokens on a
  timer — so restarting the stream needs a new one rather than a replay of the
  dead one. Callers must present Home Assistant auth or the camera's *current*
  access token: an expired token deliberately cannot mint its own successor, so
  a page that has gone stale needs a credentialed caller to refresh it
- an authenticated local Sync Module clip viewer at
  `/api/blink_liveview_proxy/clips/viewer`
- authenticated local Sync Module clip metadata, download and thumbnail
  routes under `/api/blink_liveview_proxy/clips`. Downloads forward byte
  ranges, so the viewer can seek and Safari can play them
- a manual source snapshot refresh route at
  `/api/blink_liveview_proxy/cameras/{slug}/snapshot-refresh`

The companion YAML package enables HA `stream:`. This integration exposes
`binary_sensor.blink_liveview_proxy` from the proxy `/health` endpoint.

Use the normal Blink integration for snapshots, battery, temperature, motion,
and cloud/local clip services. Use these live camera entities only when you
actually want to open live view.

The live camera entities feed Home Assistant from the proxy's raw MPEG-TS
endpoint. Home Assistant still presents HLS to the browser, but we avoid nesting
one HLS playlist inside another. The entities also return an animated local
loading frame over the matching normal Blink snapshot for still-image requests
so camera dialogs do not start as a white panel or wake battery cameras just to
refresh a dashboard thumbnail.

For smoother dashboard/tablet live view, prefer the direct player URL instead of
the native Home Assistant camera dialog. The player proxies raw MPEG-TS through
Home Assistant from the local proxy and uses a browser MSE player, avoiding HA's
stream worker and its generated `/api/hls/...` playlists.
The MSE player library is served from the custom integration itself:
`/api/blink_liveview_proxy/assets/mpegts.min.js`.

For a dashboard modal, load this as a Lovelace module resource:

```text
/api/blink_liveview_proxy/assets/blink-liveview-dialog.js
```

Then use `fire-dom-event` from `custom:button-card`:

```yaml
tap_action:
  action: fire-dom-event
  blink_liveview_proxy:
    slug: driveway
    entity_id: camera.blink_live_driveway
    title: Blink Live Driveway
```

The helper opens the direct player in an iframe dialog and passes the live
camera entity's `access_token` into the player URL. The player dialog also has
a Clips button that opens the local Sync Module clip viewer for that camera.

To open the local clip viewer directly from a card, use:

```yaml
tap_action:
  action: fire-dom-event
  blink_liveview_proxy_clips:
    title: Blink Local Clips
```

The viewer opens on every camera and its Camera select filters from there. Add
`slug: driveway` to open with that camera's token; the list still starts on all.
A Source select picks the Sync Module, Blink's cloud, or both, and the choice is
remembered in the browser. The newest six cloud clips show a thumbnail
automatically; the rest load on demand.

To request a fresh normal Blink snapshot without starting live view, use:

```yaml
tap_action:
  action: fire-dom-event
  blink_snapshot_refresh:
    slug: driveway
```

## Blink Live View Proxy Panel

The sidebar panel has four tabs: **Overview**, **Cameras & entities**,
**Authentication**, and **YAML**. Overview shows proxy health and versions and,
for systemd or add-on installs, offers the same confirmation-gated update as a
Repairs Fix button. Cameras & entities shows model, serial, network, live view,
clips, snapshot refresh, push-to-talk availability, and every entity attached
to the official Blink device. Selecting an entity opens Home Assistant's native
More Info control. YAML generates a whole dashboard, one view, or a card from
the current inventory and copies it without exposing the proxy token.

The Authentication tab drives the proxy's login state machine from the
browser. It requires a proxy API token configured on the proxy and entered in
this integration; without one the proxy refuses the routes entirely.

1. Open **Blink Proxy → Authentication** as a Home Assistant administrator.
2. Select **Reauthenticate** when a working session already exists.
3. Enter the Blink email and password, then start the login.
4. When the page shows **waiting for PIN**, enter the PIN Blink just issued.
5. Wait for **success**.

States shown: idle, authenticating, waiting for PIN, success, expired, failure.
One attempt runs at a time, a PIN is only accepted for the challenge that asked
for it, an unanswered challenge expires, cancellation is explicit, and a failed
attempt leaves the previously working Blink session serving live views.

The page holds nothing: no proxy token, no credentials, no PIN, and no browser
storage. It calls `/api/blink_liveview_proxy/auth/*`, which requires an
authenticated administrator and adds the bearer token server-side. Restarting
the proxy drops an in-flight challenge, so start a new login afterwards.

## Packaging Notes

This is packageable as two pieces:

- the Home Assistant custom integration under `custom_components/`
- the local proxy service that owns Blink login, token refresh, IMMI bridging,
  and clip access

The custom integration discovers cameras through the proxy's `/cameras`
endpoint. The proxy can discover cameras without a JSON camera map, but a map is
recommended so stable slugs can be matched to known Blink entity IDs for
dashboard snapshots. The Home Assistant UI intentionally treats local Sync
Module clips as the primary clip surface; Blink cloud clips may exist for some
accounts, but they are not exposed in the HA viewer.

Push-to-talk is currently experimental. The direct player shows a hold-to-talk
button once video is playing, tunnels microphone PCM through Home Assistant to
the proxy, and has the proxy encode AAC/IMMI audio with ffmpeg. Browser
microphone capture requires HTTPS or another trusted browser origin; plain
`http://homeassistant.local:8123` is expected to block the mic even though live video
still works.
