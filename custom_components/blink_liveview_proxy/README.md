# Blink Liveview Proxy Custom Integration

Local Home Assistant wrapper for the Blink Liveview Proxy service.

This integration does not log in to Blink and does not store Blink credentials.
It only talks to the local proxy HTTP API:

- `GET /health`
- `GET /cameras`
- `GET /cameras/{slug}/mpegts`
- `GET /clips?source=local`
- `GET /clips/{clip_id}.mp4?source=local`

The proxy remains responsible for Blink OAuth, two-factor login, token refresh,
the `immis://` bridge, and HLS generation.

The publishable package lives at `blink-liveview-proxy/` in this repo.

## Local Test Run

From the repo root:

```bash
. .venv-blink-liveview/bin/activate
python blink-liveview-proxy/proxy/blink_liveview_proxy.py --config /path/to/config.json serve
```

If `BLINK_PROXY_TOKEN` is set for the proxy, enter the same token in the
integration setup form.

For first-time Blink auth, run the proxy CLI once before starting `serve`:

```bash
python blink-liveview-proxy/proxy/blink_liveview_proxy.py --config /path/to/config.json list
```

Enter the Blink password and 2FA code when prompted. The proxy stores the Blink
refresh token in `secrets/blink-auth.json`; this integration only sees the local
proxy URL.

## Home Assistant Setup

After the custom component is present under Home Assistant's
`custom_components/` directory and Home Assistant has restarted:

1. Go to Settings > Devices & services.
2. Add integration: `Blink Liveview Proxy`.
3. Use `http://127.0.0.1:8088` when the proxy runs on the HA host.
4. Use `http://<mac-lan-ip>:8088` when testing against a proxy running on the
   Mac with `--host 0.0.0.0`.
5. Set the live-view duration. The default is 60 seconds; valid values are
   10-300 seconds.

The integration creates:

- one `camera.blink_live_*` stream entity per proxy camera
- authenticated direct browser player URLs at
  `/api/blink_liveview_proxy/cameras/{slug}/player`
- an authenticated local Sync Module clip viewer at
  `/api/blink_liveview_proxy/clips/viewer`
- authenticated local Sync Module clip metadata/download routes under
  `/api/blink_liveview_proxy/clips`
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
`/api/blink_liveview_proxy/static/mpegts.min.js`.

For a dashboard modal, load this as a Lovelace module resource:

```text
/api/blink_liveview_proxy/static/blink-liveview-dialog.js
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

To request a fresh normal Blink snapshot without starting live view, use:

```yaml
tap_action:
  action: fire-dom-event
  blink_snapshot_refresh:
    slug: driveway
```

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
