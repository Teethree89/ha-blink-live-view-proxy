# Blink Live View Proxy — Add-on

Runs the Blink Live View Proxy as a Home Assistant add-on. No separate Linux host required.

## Prerequisites

- The official **Blink** integration installed and working in Home Assistant.
- The **Blink Live View Proxy** custom integration installed via HACS (or copied from `custom_components/` in this repo).

## Installation

1. Add this repository as a custom add-on repository:
   `Settings → Add-ons → Add-on Store → ⋮ → Repositories`
   ```
   https://github.com/Teethree89/ha-blink-live-view-proxy
   ```

2. Install **Blink Live View Proxy** from the add-on store and open its **Configuration** tab.

## Requirements

| What | Why |
|---|---|
| Home Assistant OS or Supervised | Add-ons need Supervisor. On Container or Core, run the proxy as a systemd service or a Docker container instead |
| `aarch64` or `amd64` | ffmpeg transcoding a live stream is beyond 32-bit ARM; those builds were dropped in 0.2.0 |
| A Blink account with cameras | The add-on logs in as you, and Blink texts a 2FA PIN the first time |
| The matching custom integration | Only if you want the Home Assistant side: entities, the player, the clip viewer, and the **Blink Live View Proxy** panel |
| The official Blink integration | Optional. It adds the snapshot behind the loading frame, the snapshot-refresh button and motion switches. Everything else works without it |

ffmpeg and Python are inside the add-on image; there is nothing to install on
the host.

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `blink_username` | — | Your Blink account email |
| `blink_password` | — | Your Blink account password |
| `blink_2fa_code` | — | Live PIN handoff while the running add-on is waiting; leave empty before login and clear after success |
| `proxy_api_token` | generated | Leave empty. The add-on generates one on first start, keeps it in `/data/proxy-token` across restarts and updates, and shares it with the HA integration. Set it only to pin a token of your own. |
| `port` | `8088` | Port the proxy HTTP API listens on |
| `low_latency` | `false` | Re-encode the live view into one-second segments so it starts several seconds sooner. Costs CPU for each open stream; see [Low latency](../docs/CONFIGURATION.md#low-latency) |
| `cameras` | `[]` | List of camera entries (see below) |
| `ptt_disabled_product_types` | empty | Blink product types that are never offered Hold Talk, e.g. `owl`. Empty keeps the proxy's own default rather than allowing everything |
| `ptt_disabled_camera_types` | empty | The same, by camera type, e.g. `mini`. Empty keeps the proxy's default |
| `ptt_force_enabled_slugs` | empty | Cameras that are offered Hold Talk whatever the two lists above say |

Push-to-talk needs Blink's IMMI transport. Cameras that stream over RTSP have
no talk path at all, so listing their product type in
`ptt_disabled_product_types` is what stops the button being offered where it
can only fail. Each list is left empty by default and an empty list means
"use the proxy's default", so clearing a box never turns push-to-talk on for a
family that cannot use it.

### Camera fields

| Field | Required | Description |
|-------|----------|-------------|
| `slug` | Yes | Short identifier used in URLs, e.g. `driveway` |
| `entity_id` | Yes | HA camera entity, e.g. `camera.blink_driveway` |
| `name` | No | Display name |
| `id` | No | Blink camera ID (improves clip lookup) |
| `serial` | No | Blink camera serial (improves clip lookup) |

Example:

```yaml
cameras:
  - slug: driveway
    entity_id: camera.blink_driveway
    name: Driveway
  - slug: back_door
    entity_id: camera.blink_back_door
    name: Back Door
```

## First-Time Login (One Running Process)

Blink sends a 2FA PIN to your phone or email **in response to your credentials** — you
cannot know the PIN before starting the add-on for the first time. The flow is:

1. Set `blink_username` and `blink_password`. Leave `blink_2fa_code` and
   `proxy_api_token` empty, save, then start the add-on once.
2. Keep that process running. The add-on sends the credentials to Blink and the
   log changes from authenticating to waiting for a PIN.
3. Check the phone/email associated with Blink for the **new PIN issued by this
   attempt**. It expires in a few minutes.
4. While the same process is waiting, paste the PIN into `blink_2fa_code` and
   save the options. The proxy polls the live Supervisor option and submits it
   without a restart.
5. Wait for the log to report success, then clear `blink_2fa_code`. The auth
   cache is saved at `/data/blink-auth.json` and reused automatically.

Do **not** restart after saving the PIN. A restart destroys the OAuth challenge
session that requested it; the old PIN cannot authenticate the new session. Do
not pre-fill a PIN before starting either—the proxy intentionally ignores
start-time PIN values because they are stale.

If authentication expires later, repeat these steps with a newly issued PIN.

## Browser Reauthentication

The add-on writes its token to `blink_liveview_proxy.token` in the Home
Assistant config directory, and the integration's setup form pre-fills from it —
nothing to copy. Upgrading from a version that ran without a token is handled
the same way: the proxy starts requiring one, Home Assistant notices the
rejected requests and asks you to confirm the new token, already filled in. Once that integration is added, Home Assistant shows **Blink Live View
Proxy** in the sidebar for administrators; its **Authentication** tab is the login page:

1. Open the panel and select **Reauthenticate** (or use the login form shown for
   an idle/failed proxy).
2. Enter the Blink email/password and start login.
3. Keep the add-on running until the page shows **waiting for PIN**.
4. Enter the newly issued PIN on that same page.
5. Wait for **success**.

The proxy keeps one challenge in memory, rejects concurrent or stale attempts,
and preserves an existing working client until reauthentication succeeds.
Cancel is explicit. A timeout shows **expired**. Restarting the add-on cancels
the challenge and requires a new login/new PIN.

The panel is an admin-only Home Assistant custom panel. The browser sends
credentials/PINs only in authenticated no-store request bodies. It does not
receive the proxy token or use cookies/localStorage/sessionStorage for these
secrets; Home Assistant adds the bearer token server-side. Direct proxy auth
routes are disabled when `proxy_api_token` is empty and never accept tokens in
query strings.

## Connecting the HA Integration

After the add-on starts, add the integration:

```
Settings → Devices & Services → Add Integration → Blink Live View Proxy
```

The form arrives pre-filled with the token the add-on generated and the address
it is reachable on, so setup is usually a single click. That address is the
add-on's own hostname on Home Assistant's internal network — something like
`http://a1b2c3d4-blink-liveview-proxy:8088`, where the prefix identifies the
repository you installed from. It works with no port published to the host,
which is the default: `8088/tcp` is offered in the add-on's **Network** panel
and left unmapped unless you map it.

If the form ever arrives with an address that does not work, the two things to
try are the internal hostname above (take the slug from the add-on page's URL
and swap `_` for `-`) or mapping `8088` in the Network panel and using
`http://homeassistant.local:8088`.

If you replace `proxy_api_token` later, restart the add-on: it rewrites the
shared file, and Home Assistant prompts once to accept the new token. The
integration configures only the proxy URL, stream duration, and proxy token;
Blink account authentication stays inside the proxy.

## Storage

Two files are written outside `/data`, both into the Home Assistant config
directory and both rewritten on every start: `blink_liveview_proxy.token` and
`blink_liveview_proxy.url`, which are what the integration's setup form fills
itself in from.

Everything else the add-on keeps is under `/data`: the Blink refresh token
(`blink-auth.json`), the generated proxy token, HLS segments while a live view
is open, the last watched live view per camera, and from 0.7.0 a clip cache at
`/data/clips` — each local clip fetched from Blink once, with its first-frame
thumbnail beside it, pruned oldest-first past 512 MB.

## Health Check

From a machine on your network, with `8088` mapped in the add-on's **Network**
panel — without that mapping nothing on the host is listening, by design:

```bash
curl http://homeassistant.local:8088/health
curl http://homeassistant.local:8088/cameras
```
