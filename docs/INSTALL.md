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
3. Open the add-on **Configuration** tab. Fill in `blink_username` and
   `blink_password`. Leave `blink_2fa_code` and `proxy_api_token` empty: the
   add-on generates a token on first start, keeps it across updates, and shares
   it with the integration. Cameras are discovered, so the camera list is
   optional and only pins slugs to HA entities.
4. Start the add-on once and keep it running. When its log says Blink sent a
   PIN, enter that newly issued PIN in `blink_2fa_code` and save the options.
   Saving is enough; **do not restart the add-on**.
5. Wait for the success log, then clear `blink_2fa_code`. The refresh data is
   now in `/data/blink-auth.json` and is reused on later starts.
6. Continue from [Step 3 — Add the HA Integration](#3-add-the-ha-integration).
   The URL and token are pre-filled from what the add-on shared.

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

Use the install script (recommended). It is unattended and idempotent:

```bash
sudo scripts/install-proxy.sh
```

It installs the code and a venv, writes `/etc/blink-liveview-proxy/config.json`
with camera discovery and no sample entries, generates a proxy API token into
the service's environment file, installs the optional watchdog, then enables and
starts the service and waits for `/health`. It ends by printing the URL and the
command that reads the token back, which are the only two things Home Assistant
asks for. Re-run it to upgrade: code is replaced and the service restarted,
while the token, config, and Blink session are left alone.

Environment overrides: `BIND_HOST=127.0.0.1` keeps the proxy on loopback,
`PROXY_PORT` changes the port, `BLINK_PROXY_TOKEN` supplies your own token, and
`INSTALL_WATCHDOG=0` skips the watchdog.

Steps 2a to 2d below are what the script does. Follow them only for a manual
install — the script covers all of it except the Blink login, which needs a
human either way.

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

Run the login interactively. The command first submits the credentials; only
after Blink issues a new PIN does it prompt for that PIN in the same process:

```bash
read -r -p "Blink email: " BLINK_USERNAME
read -r -s -p "Blink password: " BLINK_PASSWORD; printf '\n'
export BLINK_USERNAME BLINK_PASSWORD
/opt/blink-liveview-proxy/.venv/bin/python \
  /opt/blink-liveview-proxy/blink_liveview_proxy.py \
  --config /etc/blink-liveview-proxy/config.json list
unset BLINK_USERNAME BLINK_PASSWORD
```

Enter the freshly issued PIN at `Blink 2FA code:`. Do not put a PIN in
`BLINK_2FA_CODE` or `--pin` before starting: a pre-supplied code belongs to an
older challenge and is intentionally ignored.

After this succeeds, the proxy will have an auth cache under
`/var/lib/blink-liveview-proxy/secrets/blink-auth.json`.

### 2b. Proxy API Token

`scripts/install-proxy.sh` already did this. Do it by hand only for a manual
install, or to replace a token deliberately.

A token is required for the browser authentication panel, and for any proxy
bound wider than `127.0.0.1`. The service reads it from an environment file, so
exporting it in a shell is not enough — `/auth/*` answers `503` while the
running service has no token.

```bash
sudo install -m 600 /dev/null /etc/blink-liveview-proxy/blink-liveview-proxy.env
printf 'BLINK_PROXY_TOKEN=%s\n' "$(openssl rand -hex 32)" \
  | sudo tee /etc/blink-liveview-proxy/blink-liveview-proxy.env >/dev/null
sudo systemctl restart blink-liveview-proxy.service
```

`systemd/blink-liveview-proxy.service` already reads that path. Read the value
back with `sudo cat` when the Home Assistant integration asks for it, and keep
it out of URLs and shell history.

### 2c. Install Systemd Service

```bash
sudo cp systemd/blink-liveview-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now blink-liveview-proxy.service
sudo systemctl status blink-liveview-proxy.service
```

Health check:

```bash
curl http://127.0.0.1:8088/health
curl http://127.0.0.1:8088/status
curl http://127.0.0.1:8088/cameras
```

### 2d. Optional Watchdog

BlinkPy can get stuck in a token-refresh retry loop that only a restart
clears. `scripts/install-proxy.sh` installs a timer that watches the journal
for that pattern and restarts the service, backing off after three restarts
in 30 minutes so it never hammers Blink's login endpoint (repeated failures
there are what triggers a burst of 2FA texts).

To install it by hand:

```bash
sudo install -m 0755 scripts/blink-liveview-proxy-watchdog.sh /usr/local/sbin/
sudo cp systemd/blink-liveview-proxy-watchdog.service /etc/systemd/system/
sudo cp systemd/blink-liveview-proxy-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now blink-liveview-proxy-watchdog.timer
```

Skip it during install with `INSTALL_WATCHDOG=0 sudo -E scripts/install-proxy.sh`.

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

The integration can be added while a new proxy is waiting for Blink login as
long as the proxy API token is configured. It adds an admin-only **Blink
Authentication** sidebar panel — that panel is the only browser entry point,
including for a systemd install: the proxy itself never serves a login page.
Home Assistant must be able to reach the proxy URL you entered, so a proxy bound
to `127.0.0.1` on another machine has no panel. For browser login or deliberate
reauthentication:

1. Open **Blink Authentication** as a Home Assistant administrator.
2. Enter the Blink email and password; select **Start login**.
3. Leave the proxy running while Blink sends a new PIN.
4. Enter that PIN on the same page before the challenge expires.
5. Wait for **success**. The existing `auth_file` is replaced atomically only
   after the candidate login succeeds.

The panel contains no proxy token. Home Assistant authenticates the admin and
adds the configured bearer token on its server-side request to the proxy. No
password or PIN is placed in a URL, response, log, generated asset, or browser
persistent storage. If the service restarts, the challenge is cancelled; start
a new attempt and use the newly issued PIN.

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
