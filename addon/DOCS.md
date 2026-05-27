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

## First-Time Login

On first start with `blink_2fa_code` set, the add-on authenticates with Blink and saves an auth
token to persistent storage (`/data/blink-auth.json`). The log will confirm success.

After that, clear `blink_2fa_code` from options — the saved token is reused automatically.
If the token ever expires, set a fresh 2FA code and restart the add-on.

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
