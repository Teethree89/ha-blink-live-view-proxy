# Install Guide

The official Blink integration is **recommended but not required** — see
[Alongside the official Blink integration](#alongside-the-official-blink-integration)
for exactly what it adds and what stops working without it.

## Which install am I?

Two pieces get installed: the **proxy**, which logs in to Blink and does the
work, and the **integration**, which is how Home Assistant talks to it. The
integration is always the same — HACS, restart, add it. Only the proxy differs.

| Your Home Assistant | Run the proxy as | Section |
|---|---|---|
| Home Assistant OS or Supervised | An add-on | [A](#a--home-assistant-os-or-supervised) |
| Container or Core, on a Linux host with systemd | A systemd service | [B](#b--linux-host-with-systemd) |
| Anything else — a NAS, a Docker-only host | A Docker container | [C](#c--docker-anywhere) |
| Proxy on a different machine from HA | Either B or C, on that machine | [B](#b--linux-host-with-systemd) / [C](#c--docker-anywhere) |

Whichever you pick, these happen without you: the proxy API token, a config
that discovers your cameras, and the dashboard helper resource. One step is
irreducible in every path — **the Blink 2FA PIN**. Blink only issues it after a
sign-in starts and it dies with that session, so no installer can supply it in
advance. You will type it once, into the page or option that is waiting for it.

Then everyone continues from
[Step 3 — Add the HA Integration](#3-add-the-ha-integration).

---

## A — Home Assistant OS or Supervised

The add-on is the whole proxy. No Linux setup, no Python, no separate host.

1. Go to `Settings → Add-ons → Add-on Store → ⋮ → Repositories` and add:
   ```
   https://github.com/Teethree89/ha-blink-live-view-proxy
   ```
2. Install **Blink Live View Proxy** from the store.
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

## B — Linux host with systemd

### 1. Install Proxy Prerequisites

On the host that will run the proxy:

```bash
apt-get update
apt-get install -y python3 python3-venv ffmpeg
```

Python **3.10 or newer** — `blinkpy` and `aiohttp` both require it, and the
installer stops with a clear message rather than letting pip fail obscurely.
Debian 12 and Ubuntu 22.04 or newer are fine; Debian 11 ships 3.9 and is not.
`ffmpeg` does the HLS packaging and the push-to-talk encode. The installer adds
whichever of these is missing on apt systems, so this step is only needed for a
manual install.

### 2. Install Proxy Files

Use the install script (recommended). It is unattended and idempotent:

```bash
sudo scripts/install-proxy.sh
```

It installs the code and a venv, writes `/etc/blink-liveview-proxy/config.json`
with camera discovery and no sample entries, generates a proxy API token into
the service's environment file, installs the on-demand updater and optional
watchdog, then enables and starts the service and waits for `/health`. It ends
by printing the URL and the command that reads the token back, which are the
only two things Home Assistant asks for. Re-run it to upgrade: code is replaced
and the service restarted, while the token, config, and Blink session are left
alone.

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

### 2b-1. Upgrading later

One line, which is also the install line — it keeps a checkout on the host,
moves it to the newest tag, and runs the installer from there:

```bash
curl -fsSL https://raw.githubusercontent.com/Teethree89/ha-blink-live-view-proxy/main/scripts/bootstrap.sh | sudo bash
```

It exits with `Already on X.Y.Z - nothing to do` when there is nothing to do,
so it is safe to run whenever. Stable and prerelease tags are both considered;
a final release wins over its prereleases, and an automatic run never
downgrades. `VERSION=0.7.0-rc.1` pins either kind of tag, while `FORCE=1`
reinstalls the current one.

The installer also places `blink-liveview-proxy-update.service` on the host.
When the Home Assistant integration is newer than the proxy, its repair notice
offers a **Fix** button that starts this unit. The request chooses no tag,
branch, or repository: the host-side updater always selects the newest release
tag. A proxy from before on-demand update support cannot expose that button, so
run the bootstrap line above once to seed the updater and endpoint.

To let it run on a schedule too, enable the timer once:

```bash
INSTALL_AUTOUPDATE=1 sudo scripts/install-proxy.sh
```

That enables `blink-liveview-proxy-update.timer`, which runs the same check
nightly with an hour of jitter and installs a new tag when there is one. It is
off by default: it restarts the camera proxy when it fires, and that should be
a decision, not a surprise. Disabling the timer does not remove the on-demand
Fix button. `systemctl disable --now blink-liveview-proxy-update.timer` turns
the schedule back off.

Doing it by hand instead needs a checkout of the repository *on the proxy host*
— a fresh install usually does not have one, so the first upgrade starts by
making it:

```bash
mkdir -p /opt/src
git clone https://github.com/Teethree89/ha-blink-live-view-proxy \
  /opt/src/ha-blink-live-view-proxy
git -C /opt/src/ha-blink-live-view-proxy checkout v0.3.0
/opt/src/ha-blink-live-view-proxy/scripts/install-proxy.sh
```

Afterwards, upgrading is `git -C /opt/src/ha-blink-live-view-proxy fetch --tags`,
a `checkout` of the tag you want, and the script again. The script finds its own
repository root, so it can be run from anywhere by full path.

> [!WARNING]
> **Do not upgrade by copying files over the old ones.** `requirements.txt`
> pins blinkpy, and 0.3.0 raised that pin from 0.25.5 to 0.25.9. Copied files
> land on the old virtualenv, so the proxy starts, imports cleanly, serves live
> view — and then fails 2FA, because 0.25.5 reads Blink's challenge response as
> a failed login. The installer updates the virtualenv; a file copy does not.

If you paste those commands into a terminal while `ssh` is still connecting,
they land in the type-ahead buffer and are discarded when the remote shell
starts. Nothing runs, and the only sign is a prompt in the wrong directory. Run
them at a live prompt, or pass the whole block to `ssh` as one argument.

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

## C — Docker, anywhere

For hosts with neither Supervisor nor systemd: a NAS, a Docker-only box, Home
Assistant Container on something that is not Debian. The image carries the
proxy and ffmpeg, and configures itself on first start.

```bash
curl -fsSLO https://raw.githubusercontent.com/Teethree89/ha-blink-live-view-proxy/main/docker-compose.example.yml
printf 'BLINK_USERNAME=you@example.com\nBLINK_PASSWORD=your-password\n' > blink.env
chmod 600 blink.env
docker compose -f docker-compose.example.yml up -d
```

Or without compose:

```bash
docker run -d --name blink-liveview-proxy --restart unless-stopped \
  -p 8088:8088 -v blink-proxy-data:/data \
  -e BLINK_USERNAME=you@example.com -e BLINK_PASSWORD=your-password \
  ghcr.io/teethree89/ha-blink-live-view-proxy:latest
```

On first start it writes `/data/config.json` (cameras are discovered, so there
is nothing to fill in) and generates a proxy API token. Read the token for the
integration:

```bash
docker exec blink-liveview-proxy cat /data/proxy-token
```

Keep `/data` on a volume. It holds the Blink refresh token, the proxy token,
the config, and the caches — losing it means logging in to Blink again.

Upgrading is `docker compose pull && docker compose up -d`, or `docker pull` and
recreate. Your `/data` survives.

**The 2FA PIN, in a container.** There is no terminal to prompt on, so use the
**Blink Proxy → Authentication** tab once the integration is installed. If you would
rather not wait for that, drop the PIN into the volume while the container is
waiting for it:

```bash
docker exec blink-liveview-proxy sh -c 'echo 123456 > /data/blink_2fa_pin.txt'
```

The proxy reads it within seconds and deletes the file.

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
Settings → Devices & services → Add integration → Blink Live View Proxy
```

The URL depends on where the proxy runs:

| Proxy | URL |
|---|---|
| Add-on | `http://homeassistant.local:8088` — pre-filled, along with the token |
| systemd or Docker on the HA host | `http://127.0.0.1:8088` if HA uses host networking, otherwise the host's name |
| Another machine | `http://<that-host>:8088` |

Paste the proxy API token in the token field. Add-on installs have it filled in
already; the other paths printed where to read it.

The integration can be added while a new proxy is waiting for Blink login as
long as the proxy API token is configured. It adds an admin-only **Blink Live View
Proxy → Authentication** sidebar tab — that panel is the only browser entry point,
including for a systemd install: the proxy itself never serves a login page.
Home Assistant must be able to reach the proxy URL you entered, so a proxy bound
to `127.0.0.1` on another machine has no panel. For browser login or deliberate
reauthentication:

1. Open **Blink Proxy → Authentication** as a Home Assistant administrator.
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

## Alongside the official Blink integration

They are independent. This project never calls Blink through the official
integration, and the official integration never calls this proxy. Live view,
clips, push-to-talk, the direct player and the authentication panel all work
with it absent.

What having it adds:

| Feature | Without the official integration |
|---|---|
| Snapshot behind the live-view loading frame | A generic loading image instead of your camera's last still |
| **Snapshot refresh** button | Unavailable — it calls `blink.trigger_camera`, which that integration owns |
| Motion detection toggles in the example dashboards | The `switch.*_camera_motion_detection` entities do not exist |
| Battery, temperature, motion sensors | Not provided here — this project deliberately does not duplicate them |

The link between the two is the `entity_id` in your camera map: point a proxy
slug at the official camera entity and the snapshot features find it.

**One account, two sessions.** Each logs in to Blink separately, with its own
device id and its own refresh token, so re-authenticating one does nothing to
the other. Blink's rate limits, though, are per *account*: a reload loop on the
official integration can exhaust them and make this proxy's next login fail too.
That is the failure described in
[OPERATIONS.md](OPERATIONS.md#reload-restart-re-auth-are-not-interchangeable),
and it is worth reading before adding automations that reload either one.

## 4. Add Lovelace Helper Resource

Add a JavaScript module resource in your dashboard:

```text
/api/blink_liveview_proxy/assets/blink-liveview-dialog.js
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
  `/api/blink_liveview_proxy/assets/blink-liveview-dialog.js`.
