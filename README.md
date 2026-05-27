# Blink Liveview Proxy

Unofficial Home Assistant custom integration plus a small local Blink proxy
service for direct Blink live view, push-to-talk experiments, last-live-view
downloads, and local Sync Module clip browsing.

This project exists because the official Home Assistant Blink integration is
good for snapshots, motion switches, arming, sensors, and normal Blink services,
but it does not expose Blink's `immis://` live-view stream. The proxy uses
BlinkPy to log in to Blink with your own account, request a live-view session,
read Blink's IMMI framing, and expose browser/HA-friendly endpoints on your LAN.

It is an interoperability project for cameras you own. It is not affiliated
with, endorsed by, or supported by Amazon or Blink.

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
addon/                                   Home Assistant add-on (HAOS one-click install)
addon/proxy/                             Proxy source bundled for the add-on container
custom_components/blink_liveview_proxy/  Home Assistant custom integration
proxy/blink_liveview_proxy.py            Compatibility CLI entrypoint
proxy/blink_proxy/                       Modular proxy implementation
proxy/config.example.json                Generic proxy config template
systemd/blink-liveview-proxy.service     Example Linux service unit
scripts/install-proxy.sh                 Linux install helper
examples/                                HA package and Lovelace snippets
docs/                                    Setup, configuration, and notes
```

## Install Options

### Option A — Home Assistant Add-on (HAOS / easiest)

If you run Home Assistant OS or Supervised, install the proxy as an add-on
directly from the add-on store. No separate Linux host or Python setup required.

1. In HA go to `Settings → Add-ons → Add-on Store → ⋮ → Repositories` and add
   this repo URL.
2. Install **Blink Liveview Proxy** from the add-on store.
3. Open the add-on **Configuration** tab, fill in your Blink credentials, 2FA
   code, and camera list, then start the add-on.
4. Install the HA integration (via HACS or manually — see below) and point it at
   `http://homeassistant.local:8088`.

See [addon/DOCS.md](addon/DOCS.md) for the full add-on setup guide.

### Option B — Linux Service (Container / Supervised / bare-metal)

1. Copy `custom_components/blink_liveview_proxy` into Home Assistant's
   `custom_components/`.
2. Install and start the proxy service from `proxy/` and `systemd/`
   (or run `sudo scripts/install-proxy.sh` from this repo).
3. Restart Home Assistant.
4. Add `Blink Liveview Proxy` from `Settings → Devices & services`.
5. Use `http://127.0.0.1:8088` as the proxy URL when the proxy runs on the HA
   host.
6. Add the Lovelace helper resource:

```text
/api/blink_liveview_proxy/static/blink-liveview-dialog.js
```

Full step-by-step in the [install guide](docs/INSTALL.md).

## HACS Custom Repository

Install the Home Assistant integration half through HACS:

1. In HACS open the three-dot menu → **Custom repositories**.
2. Add `https://github.com/Teethree89/ha-blink-live-view-proxy`, category `Integration`.
3. Download it, restart Home Assistant, then add `Blink Liveview Proxy` from
   `Settings → Devices & services`.

HACS installs only `custom_components/blink_liveview_proxy`. You still need
either the add-on (Option A) or the Linux proxy service (Option B) running
alongside it.

Default HACS listing can wait until the project has wider testing, a release,
brand assets, and passing validation history.

## Proxy API Layout

The local proxy routes are documented in the
[proxy API guide](docs/API.md).
Route handlers live in `proxy/blink_proxy/routes.py`; Blink IMMI and live-view
behavior lives in `proxy/blink_proxy/blink.py`; push-to-talk lives in
`proxy/blink_proxy/ptt.py`.

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

[Frameo USB microphone guide](https://github.com/Teethree89/ha-light-panel/blob/main/docs/frameo-usb-microphone.md)
