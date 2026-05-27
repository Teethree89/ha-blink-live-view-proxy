# Install Guide

This guide assumes Home Assistant already has the official Blink integration
configured and working.

---

## Option A — Home Assistant Add-on (HAOS / easiest)

If you run Home Assistant OS or Supervised, skip the Linux setup entirely and
install the proxy as an add-on.

1. Go to `Settings → Add-ons → Add-on Store → ⋮ → Repositories` and add:
   ```
   https://github.com/Teethree89/ha-blink-live-view-proxy
   ```
2. Install **Blink Liveview Proxy** from the store.
3. Open the add-on **Configuration** tab and fill in `blink_username`,
   `blink_password`, `blink_2fa_code`, and your camera list.
4. Start the add-on. It authenticates with Blink and begins serving on port 8088.
5. Clear `blink_2fa_code` from options after the first successful start.
6. Continue from [Step 3 — Add the HA Integration](#3-add-the-ha-integration) below.

See [addon/DOCS.md](../addon/DOCS.md) for full add-on configuration details.

---

## Option B — Linux Service (Container / Supervised / bare-metal)

### 1. Install Proxy Prerequisites

On the host that will run the proxy:

```bash
apt-get update
apt-get install -y python3 python3-venv ffmpeg
```

### 2. Install Proxy Files

Use the install script (recommended):

```bash
sudo scripts/install-proxy.sh
```

Or manually — recommended Linux layout:

```text
/opt/blink-liveview-proxy/              code + venv
/etc/blink-liveview-proxy/config.json   local config
/var/lib/blink-liveview-proxy/          auth cache, HLS, live-view cache
```

```bash
sudo mkdir -p /opt/blink-liveview-proxy
sudo cp proxy/blink_liveview_proxy.py /opt/blink-liveview-proxy/
sudo cp -R proxy/blink_proxy /opt/blink-liveview-proxy/
sudo cp proxy/requirements.txt /opt/blink-liveview-proxy/
sudo python3 -m venv /opt/blink-liveview-proxy/.venv
sudo /opt/blink-liveview-proxy/.venv/bin/python -m pip install -r /opt/blink-liveview-proxy/requirements.txt

sudo mkdir -p /etc/blink-liveview-proxy /var/lib/blink-liveview-proxy/secrets
sudo cp proxy/config.example.json /etc/blink-liveview-proxy/config.json
sudo chmod 600 /etc/blink-liveview-proxy/config.json
```

Edit `/etc/blink-liveview-proxy/config.json`.

### 2a. First Blink Login

Run once interactively or pass a current 2FA code:

```bash
BLINK_USERNAME="you@example.com" \
BLINK_PASSWORD="your-password" \
BLINK_2FA_CODE="123456" \
/opt/blink-liveview-proxy/.venv/bin/python \
  /opt/blink-liveview-proxy/blink_liveview_proxy.py \
  --config /etc/blink-liveview-proxy/config.json list
```

After this succeeds, the proxy will have an auth cache under
`/var/lib/blink-liveview-proxy/secrets/blink-auth.json`.

### 2b. Install Systemd Service

```bash
sudo cp systemd/blink-liveview-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now blink-liveview-proxy.service
sudo systemctl status blink-liveview-proxy.service
```

Health check:

```bash
curl http://127.0.0.1:8088/health
curl http://127.0.0.1:8088/cameras
```

---

## 3. Add the HA Integration

**Via HACS (recommended):**

1. Open HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/Teethree89/ha-blink-live-view-proxy`, category `Integration`.
3. Download it and restart Home Assistant.

**Manually:**

```bash
cp -R custom_components/blink_liveview_proxy /opt/homeassistant/custom_components/
```

For Docker-based HA, copy into the mounted config directory then restart the container.

After restarting:

```text
Settings → Devices & services → Add integration → Blink Liveview Proxy
```

Use `http://127.0.0.1:8088` if the proxy runs on the HA host, or
`http://homeassistant.local:8088` for the add-on.

## 4. Add Lovelace Helper Resource

Add a JavaScript module resource in your dashboard:

```text
/api/blink_liveview_proxy/static/blink-liveview-dialog.js
```

This helper opens live view and clips in dashboard dialogs.

## 5. Optional HA Package

The package in `examples/homeassistant-package.yaml` enables HA `stream:`.
The custom integration provides its own health binary sensor.

Copy it into your HA packages folder if your config does not already enable
`stream:`.

## Deploy Checklist

Before publishing or installing a fresh copy:

```bash
python3 -m py_compile custom_components/blink_liveview_proxy/*.py
python3 -m py_compile proxy/blink_liveview_proxy.py proxy/blink_proxy/*.py
node --check custom_components/blink_liveview_proxy/frontend/blink-liveview-dialog.js
```

Also confirm:

- `ffmpeg` is installed on the proxy host (or add-on handles this automatically).
- `proxy/config.json` is local-only and not committed.
- The proxy health endpoint works.
- Home Assistant can reach the proxy URL.
- Dashboard resources point at
  `/api/blink_liveview_proxy/static/blink-liveview-dialog.js`.
