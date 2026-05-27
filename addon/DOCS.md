# Blink Liveview Proxy — Add-on

Runs the Blink Liveview Proxy as a Home Assistant add-on. No separate Linux host required.

## Prerequisites

- The official **Blink** integration installed and working in Home Assistant.
- The **Blink Liveview Proxy** custom integration installed via HACS (or copied from `custom_components/` in this repo).

## Installation

1. Add this repository as a custom add-on repository:
   `Settings → Add-ons → Add-on Store → ⋮ → Repositories`
   Paste the URL of this GitHub repository.

2. Install **Blink Liveview Proxy** from the add-on store and open its **Configuration** tab.

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `blink_username` | — | Your Blink account email |
| `blink_password` | — | Your Blink account password |
| `blink_2fa_code` | — | One-time 2FA PIN (only needed for the first start) |
| `port` | `8088` | Port the proxy HTTP API listens on |
| `cameras` | `[]` | List of camera entries (see below) |

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

## First-Time Login (Two-Start Flow)

Blink sends a 2FA PIN to your phone or email **in response to your credentials** — you
cannot know the PIN before starting the add-on for the first time. The flow is:

1. **First start** — set `blink_username` and `blink_password`, leave `blink_2fa_code` empty.
   The add-on sends your credentials to Blink, Blink texts/emails you a PIN, and the
   add-on logs instructions then exits cleanly.

2. **Check your phone/email** for the Blink 2FA PIN. It expires in a few minutes.

3. **Update options** — paste the PIN into `blink_2fa_code`, save.

4. **Restart the add-on** — it submits the PIN, completes authentication, and saves the
   auth token to `/data/blink-auth.json`.

From then on, the saved token is reused automatically. You can clear `blink_2fa_code`
from options — it is no longer needed unless the token expires.

If the token ever expires, repeat from step 1.

## Connecting the HA Integration

After the add-on starts, add the integration:

```
Settings → Devices & Services → Add Integration → Blink Liveview Proxy
```

Use `http://homeassistant.local:8088` (or `http://127.0.0.1:8088`) as the proxy URL.

## Health Check

```bash
curl http://homeassistant.local:8088/health
curl http://homeassistant.local:8088/cameras
```
