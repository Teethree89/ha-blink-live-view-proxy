# Blink Liveview Proxy

Home Assistant custom integration plus a small local Blink proxy service for
direct Blink live view, push-to-talk experiments, last-live-view downloads, and
local Sync Module clip browsing.

This project exists because the official Home Assistant Blink integration is
good for snapshots, motion switches, arming, sensors, and normal Blink services,
but it does not expose Blink's `immis://` live-view stream. The proxy uses
BlinkPy to log in to Blink, request a live-view session, read Blink's IMMI
framing, and expose browser/HA-friendly endpoints on your LAN.

If this saves you a little time, [buy me a coffee](https://paypal.me/ABPaintball/5). Add `Buy me a coffee` in the PayPal note so I know what it was for.

[![Buy me a coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-$5%20PayPal-00457C?logo=paypal)](https://paypal.me/ABPaintball/5)

## What Works

- Live view through a direct MSE player.
- Configurable direct player duration, default `60` seconds.
- "End & Save" and "Save MP4" for the most recent watched live view.
- Push-to-talk on tested regular Blink cameras and doorbells.
- PTT hidden on Blink Mini/`owl` cameras by default.
- Fresh snapshot button using the official HA Blink camera entity.
- Per-camera motion detection controls when the official Blink integration
  exposes `switch.*_camera_motion_detection`.
- Local Sync Module clip viewer/downloader.
- HTTPS-friendly browser microphone flow when HA is served through a trusted
  local HTTPS origin.

## Known Limits

- This is not an official Amazon/Blink integration.
- Blink cloud clip browsing is intentionally not surfaced in HA.
- Motion zones and deeper camera settings are out of scope for now.
- Push-to-talk is experimental and model-sensitive.
- Live view still depends on Blink cloud APIs and camera/cloud limits.
- The proxy is a separate service; the HA custom integration does not log in to
  Blink by itself.

## Project Layout

```text
custom_components/blink_liveview_proxy/  Home Assistant custom integration
proxy/blink_liveview_proxy.py            Local Blink IMMI proxy service
proxy/config.example.json                Generic proxy config template
systemd/blink-liveview-proxy.service     Example Linux service
examples/                                HA package and Lovelace snippets
docs/                                    Setup, configuration, and notes
```

## Deployability Status

This folder is intended to be publishable as a standalone repo.

The package includes:

- a Home Assistant custom integration under `custom_components/`;
- bundled frontend assets for the dashboard dialog/player;
- the standalone Python proxy under `proxy/`;
- a systemd unit template;
- example HA package and Lovelace snippets;
- install, configuration, development, and limitation docs.

Two deployment pieces are still intentionally separate:

- Home Assistant installs the custom integration.
- A local Linux service runs the Blink proxy and owns Blink authentication.

That split is deliberate. The HA integration should not store Blink account
credentials or run the IMMI socket reader inside Home Assistant.

## Prerequisites

- Home Assistant with the official Blink integration already configured.
- A host that can run Python 3.11+ and `ffmpeg`.
- BlinkPy dependency from `proxy/requirements.txt`.
- HTTPS or a trusted browser origin for microphone access.

The recommended HA setup runs the proxy on the Home Assistant host and listens
only on `127.0.0.1:8088`; HA reaches it locally.

## Quick Install Shape

1. Copy `custom_components/blink_liveview_proxy` into Home Assistant's
   `custom_components/`.
2. Install and start the proxy service from `proxy/` and `systemd/`.
3. Restart Home Assistant.
4. Add `Blink Liveview Proxy` from Settings > Devices & services.
5. Use `http://127.0.0.1:8088` as the proxy URL when the proxy runs on the HA
   host.
6. Add the Lovelace helper resource:

```text
/api/blink_liveview_proxy/static/blink-liveview-dialog.js
```

Detailed setup lives in [docs/INSTALL.md](docs/INSTALL.md).

## Dashboard Helper

Use `fire-dom-event` from `custom:button-card`:

```yaml
tap_action:
  action: fire-dom-event
  blink_liveview_proxy:
    slug: front_door
    entity_id: camera.blink_live_front_door
    title: Blink Live Front Door
```

Local clips:

```yaml
tap_action:
  action: fire-dom-event
  blink_liveview_proxy_clips:
    slug: front_door
    entity_id: camera.blink_live_front_door
    title: Front Door Clips
```

Snapshot refresh:

```yaml
tap_action:
  action: fire-dom-event
  blink_snapshot_refresh:
    slug: front_door
    entity_id: camera.blink_live_front_door
    source_entity_id: camera.front_door
```

## Security Notes

Bind the proxy to `127.0.0.1` unless you have a specific reason not to. If you
bind it to the LAN, set `BLINK_PROXY_TOKEN` and configure the same token in the
Home Assistant integration.

The proxy stores Blink OAuth refresh data in the configured `auth_file`. Keep
that file out of git.

## Frameo / Wall Panel Notes

Push-to-talk on Android frames is possible, but browser microphone capture
requires a trusted HTTPS origin and working Android microphone input. For the
tested Frameo USB microphone workflow, see the HA Light Panel companion docs:

```text
ha-light-panel/docs/frameo-usb-microphone.md
```
https://github.com/Teethree89/ha-light-panel
