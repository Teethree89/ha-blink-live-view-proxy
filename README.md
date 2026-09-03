<p align="center">
  <img src="https://raw.githubusercontent.com/Teethree89/ha-blink-live-view-proxy/main/docs/images/logo.png" alt="Blink Liveview Proxy" width="520">
</p>

# Blink Liveview Proxy

Unofficial Home Assistant custom integration plus a small local Blink proxy
service for direct Blink live view, push-to-talk experiments, last-live-view
downloads, and local Sync Module clip browsing.

This project exists because the official Home Assistant Blink integration is
good for snapshots, motion switches, arming, sensors, and normal Blink services,
but it does not expose Blink's live-view stream. The proxy uses BlinkPy to log
in to Blink with your own account, request a live-view session, read whichever
transport Blink hands that camera — its own `immis://` framing, or `rtsps://`
on the older `xt` and `white` models — and expose browser/HA-friendly endpoints
on your LAN.

It is an interoperability project for cameras you own. It is not affiliated
with, endorsed by, or supported by Amazon or Blink.

If this saves you a little time, [buy me a coffee](https://paypal.me/ABPaintball/5). Add `Buy me a coffee` in the PayPal note so I know what it was for.

[![Buy me a coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-$5%20PayPal-00457C?logo=paypal)](https://paypal.me/ABPaintball/5)

## What Works

- Live view through a direct MSE player.
- Both of Blink's live-view transports, so the older `xt` and `white` cameras
  work too, not only the `immis://` models.
- Optional low-latency mode: one-second HLS segments instead of four-second
  ones, at the cost of an encode per open live view.
- Configurable direct player duration, default `60` seconds.
- "End & Save" and "Save MP4" for the most recent watched live view.
- Push-to-talk on tested regular Blink cameras and doorbells.
- PTT hidden on Blink Mini/`owl` cameras by default, and unavailable on
  RTSP-transport cameras, which have no upstream audio channel.
- Fresh snapshot button using the official HA Blink camera entity.
- Per-camera motion detection controls when the official Blink integration
  exposes `switch.*_camera_motion_detection`.
- Local Sync Module clip viewer/downloader.
- Admin-only browser authentication and deliberate reauthentication through
  Home Assistant, with the Blink OAuth challenge kept in one proxy process.
- Three ways to run the proxy — add-on, systemd, or a standalone Docker image —
  each provisioning its own API token.
- HTTPS-friendly browser microphone flow when HA is served through a trusted
  local HTTPS origin.

See the [roadmap](docs/ROADMAP.md) for what is planned, what is deliberately
out of scope, and the rough edges worth knowing about.

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
systemd/blink-liveview-proxy-watchdog.*  Optional stuck-token-refresh watchdog
scripts/install-proxy.sh                 Linux install helper
scripts/blink-liveview-proxy-watchdog.sh Watchdog script installed by the helper
examples/                                HA package and Lovelace dashboards
scripts/generate-dashboard.py            Builds a dashboard from live cameras
docs/                                    Setup, configuration, and notes
```

## Requirements

Two halves with different needs. The **proxy** is what talks to Blink; the
**integration** is what Home Assistant talks to.

**The proxy**, whichever way you run it:

| Path | Needs |
|---|---|
| Add-on | Home Assistant OS or Supervised, on `aarch64` or `amd64` |
| systemd service | A Linux host with systemd, **Python 3.10+**, `ffmpeg`, and `git` for the one-liner. The installer adds the missing ones on apt systems |
| Docker | Docker on `linux/amd64` or `linux/arm64`. Everything else is inside the image |

Its Python dependencies are in [`proxy/requirements.txt`](proxy/requirements.txt):
`aiohttp`, `certifi`, and `blinkpy` pinned to an exact version — 0.25.9 is the
release that recognises Blink's current 2FA challenge, and an older one fails
login while still texting you a code.

**The integration**: Home Assistant **2024.6.0 or newer**, installed through
HACS or copied into `custom_components/`. It adds no Python dependencies of its
own — it uses Home Assistant's `aiohttp` — and depends only on built-in
`http`, `frontend` and `panel_custom`.

**Dashboards**: [button-card](https://github.com/custom-cards/button-card) for
every example, plus
[auto-entities](https://github.com/thomasloven/lovelace-auto-entities) for the
self-populating one. Both from HACS → Frontend. The player and clip viewer need
neither.

**Account and hardware**: a Blink account with cameras, a Sync Module with local
storage if you want clips, and — optional but recommended — the official Blink
integration for snapshots, motion and battery. This project deliberately does
not duplicate those.

**For development**: `pyyaml` and `proxy/requirements.txt` for the tests, `ruff`
for the lint, and `node` for `node --check` on the frontend files.

Once it is installed, the **Blink Proxy** sidebar panel checks every one of
these against the running install and keeps the setup steps for each next to
the result — see the [Dashboard Guide](docs/DASHBOARD.md#requirements-at-a-glance).

## Install Options

### Option A — Home Assistant Add-on (HAOS / easiest)

If you run Home Assistant OS or Supervised, install the proxy as an add-on
directly from the add-on store. No separate Linux host or Python setup required.

1. In HA go to `Settings → Add-ons → Add-on Store → ⋮ → Repositories` and add:
   ```
   https://github.com/Teethree89/ha-blink-live-view-proxy
   ```
2. Install **Blink Liveview Proxy** from the add-on store.
3. Open the add-on **Configuration** tab. Set `blink_username` and
   `blink_password`, leave `blink_2fa_code` and `proxy_api_token` empty, then
   start the add-on once. Cameras are discovered, and the add-on generates its
   own proxy token on first start.
4. Wait until the log says Blink sent a PIN. Paste that newly issued PIN into
   `blink_2fa_code` and save the options **while the add-on keeps running**.
   Do not restart: the PIN belongs to the OAuth session in that process.
5. After the log reports success, clear `blink_2fa_code` so it cannot be reused
   accidentally. The auth cache at `/data/blink-auth.json` handles later starts.
6. Install the HA integration (via HACS or manually — see below) and add it.
   The URL and the generated token are pre-filled from what the add-on shared,
   so setup is a click.

See [addon/DOCS.md](addon/DOCS.md) for the full add-on setup guide.

### Option B — Linux Service (systemd)

One command on the proxy host — it installs the code and venv, writes a config
that discovers cameras, generates a proxy API token, installs the on-demand
updater and watchdog, and starts the service:

```bash
curl -fsSL https://raw.githubusercontent.com/Teethree89/ha-blink-live-view-proxy/main/scripts/bootstrap.sh | sudo bash
```

That keeps a checkout on the host and installs the newest **tag**. The Home
Assistant integration can then offer a repair **Fix** button when it is newer
than the proxy; the button starts the updater installed on this host. Re-run
the line yourself to upgrade a proxy that predates that endpoint.
`VERSION=v0.3.0` pins one, and `INSTALL_AUTOUPDATE=1` enables a daily timer that
runs the same check without being asked. From a checkout you already have,
`sudo scripts/install-proxy.sh` does the same thing.

It prints the URL to give Home Assistant and the one command that reads the
token back. Then, in Home Assistant:

1. Install the integration through HACS (see below) and restart HA.
2. Add `Blink Liveview Proxy` from `Settings → Devices & services`, using the
   printed URL and token.
3. Open **Blink Proxy** in the sidebar, select **Authentication**, and sign in.

Re-running the script upgrades in place, keeping the token, config, and Blink
session. The Lovelace helper resource is registered for you; add it by hand
only in YAML-mode dashboards:

```text
/api/blink_liveview_proxy/assets/blink-liveview-dialog.js
```

Full step-by-step in the [install guide](docs/INSTALL.md), and what to do when
it misbehaves in the [operations guide](docs/OPERATIONS.md).

### Browser authentication and reauthentication

The custom integration registers an admin-only **Blink Proxy** dashboard in the
Home Assistant sidebar. Its Authentication tab needs a proxy API token on both sides, which
both install paths now provision for you: the installer writes one to the
service's environment file, and the add-on generates one and shares it with the
integration's setup form.

1. Open **Blink Proxy → Authentication** as a Home Assistant administrator.
2. Select **Reauthenticate** if a working cached session already exists.
3. Enter the Blink email and password and start login.
4. Wait for Blink to issue a new PIN, then enter it on the same page before the
   displayed challenge expires. Do not restart the proxy.
5. Wait for **success**. The proxy atomically replaces the auth cache; Home
   Assistant never stores the Blink password or PIN.

The page shows idle, authenticating, waiting-for-PIN, success, expired, and
failure states. It permits one attempt at a time, rejects stale PINs, supports
cancellation, and leaves an existing working client active if reauthentication
fails. A service restart cancels an in-memory challenge, so start a new login
and use the new PIN afterward.

### Option C — Docker (NAS, or any host without systemd)

```bash
docker run -d --name blink-liveview-proxy --restart unless-stopped \
  -p 8088:8088 -v blink-proxy-data:/data \
  -e BLINK_USERNAME=you@example.com -e BLINK_PASSWORD=your-password \
  ghcr.io/teethree89/ha-blink-live-view-proxy:latest
```

It writes its own config, discovers your cameras, and generates a proxy API
token on first start — read it with
`docker exec blink-liveview-proxy cat /data/proxy-token`. Keep `/data` on a
volume: it holds the Blink refresh token. A `docker-compose.example.yml` is in
the repository root.

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

## Dashboards

Three ready-made options, from hand-edited to self-populating, are in the
[dashboard guide](docs/DASHBOARD.md):

The admin-only **Blink Proxy → YAML** tab is the quickest option: it discovers
the current cameras and produces a whole dashboard, one view, or a paste-ready
card without asking you for the proxy URL or token. The Cameras & entities tab
also shows every entity Home Assistant associates with each Blink device and
opens native More Info controls for motion, battery, temperature, and related
features.

> [!IMPORTANT]
> The self-populating dashboard is **not standalone**. Install
> **auto-entities** and **button-card** from HACS → Frontend before using it.
> The hand-edited and generated options need **button-card**, but do not need
> auto-entities. Use the generator if you want automatic camera discovery
> without installing auto-entities.

- [`examples/lovelace-dashboard.yaml`](examples/lovelace-dashboard.yaml) — one
  commented camera with every action; copy it per camera.
- [`scripts/generate-dashboard.py`](scripts/generate-dashboard.py) — asks the
  running proxy what exists and prints a finished dashboard, a single view, or
  one card, whichever you need (`--format dashboard|view|card`).
- [`examples/lovelace-auto-populate.yaml`](examples/lovelace-auto-populate.yaml)
  — builds a tile for every camera automatically as it appears; requires
  **auto-entities** and **button-card**.

For system controls,
[`examples/homeassistant-restart-button.yaml`](examples/homeassistant-restart-button.yaml)
is a native, confirmation-gated Home Assistant restart button for admins.

Register this dashboard resource first or every tap silently does nothing:

```text
/api/blink_liveview_proxy/assets/blink-liveview-dialog.js
```

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
Home Assistant integration. The browser-auth control routes are disabled when
that token is empty and accept it only as an `Authorization: Bearer` header,
never in a URL.

The proxy stores Blink OAuth refresh data in the configured `auth_file`. Keep
that file out of git. The authentication panel sends credentials and PINs only
in no-store request bodies, does not use browser-persistent storage, and never
receives the proxy token; Home Assistant adds it server-side.

## Frameo / Wall Panel Notes

<img src="https://raw.githubusercontent.com/Teethree89/ha-blink-live-view-proxy/main/docs/images/ha-light-panel-cameras.jpg" alt="The HA Light Panel camera view: six Blink camera tiles, each with a snapshot, temperature, battery state, and Snapshot, Motion and Clips buttons" width="100%">

<sub>The camera view from
[HA Light Panel](https://github.com/Teethree89/ha-light-panel) — a separate
wall-panel project for low-power browsers and photo frames — with its tiles fed
by this proxy. It is not a Lovelace dashboard; for those see
[Dashboards](#dashboards) above.</sub>

Push-to-talk on Android frames is possible, but browser microphone capture
requires a trusted HTTPS origin and working Android microphone input. For the
tested Frameo USB microphone workflow, see the HA Light Panel companion docs:

[Frameo USB microphone guide](https://github.com/Teethree89/ha-light-panel/blob/main/docs/frameo-usb-microphone.md)

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with, endorsed by, or supported by Amazon or Blink. This is an
interoperability project for cameras you already own.
