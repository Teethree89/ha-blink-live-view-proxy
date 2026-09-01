#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPT_DIR="${OPT_DIR:-/opt/blink-liveview-proxy}"
ETC_DIR="${ETC_DIR:-/etc/blink-liveview-proxy}"
STATE_DIR="${STATE_DIR:-/var/lib/blink-liveview-proxy}"

# Prerequisites. The proxy needs ffmpeg for HLS and push-to-talk, and
# python3-venv to build its virtualenv; a fresh Debian/Ubuntu host has neither,
# and failing later on a missing binary is a poor first impression. Only apt is
# handled, and only when something is actually missing. INSTALL_DEPS=0 skips it.
if [ "${INSTALL_DEPS:-1}" = "1" ] && command -v apt-get >/dev/null 2>&1; then
  MISSING=""
  command -v ffmpeg >/dev/null 2>&1 || MISSING="ffmpeg"
  python3 -c 'import venv' >/dev/null 2>&1 || MISSING="$MISSING python3-venv"
  MISSING="${MISSING# }"
  if [ -n "$MISSING" ]; then
    echo "Installing prerequisites: $MISSING"
    apt-get update
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y $MISSING
  fi
fi

# blinkpy and aiohttp both require Python 3.10. Without this check the failure
# lands inside pip's resolver, in a message that names neither this script nor
# the version it needed.
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is required. Found: $(python3 -V 2>&1)" >&2
  exit 1
fi

install -d "$OPT_DIR" "$ETC_DIR" "$STATE_DIR/secrets"
install -m 0644 "$ROOT/proxy/blink_liveview_proxy.py" "$OPT_DIR/blink_liveview_proxy.py"
install -m 0644 "$ROOT/proxy/requirements.txt" "$OPT_DIR/requirements.txt"
rm -rf "$OPT_DIR/blink_proxy"
cp -R "$ROOT/proxy/blink_proxy" "$OPT_DIR/blink_proxy"

python3 -m venv "$OPT_DIR/.venv"
"$OPT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$OPT_DIR/.venv/bin/python" -m pip install -r "$OPT_DIR/requirements.txt"

PROXY_PORT="${PROXY_PORT:-8088}"

# Proxy config, written once and never hand-edited afterwards.
#
# The camera map is emptied deliberately: the proxy discovers cameras from the
# account, and the example's sample slug would otherwise appear as a camera
# that never streams. The default bind is every interface, because Home
# Assistant usually runs on a different host and a token is always provisioned
# below; BIND_HOST=127.0.0.1 keeps it loopback-only.
if [ ! -f "$ETC_DIR/config.json" ]; then
  ( umask 077
    python3 - "$ROOT/proxy/config.example.json" "${BIND_HOST:-0.0.0.0}" "$PROXY_PORT" \
      > "$ETC_DIR/config.json" <<'PYCONF'
import json
import sys

config = json.load(open(sys.argv[1]))
config["host"] = sys.argv[2]
config["port"] = int(sys.argv[3])
config["cameras"] = {}
json.dump(config, sys.stdout, indent=2)
print()
PYCONF
  )
  chmod 0600 "$ETC_DIR/config.json"
fi

# Proxy API token. The unit reads this file through EnvironmentFile=, and the
# browser authentication routes refuse to run without a token, so provision one
# here instead of leaving a manual step that is easy to skip.
#
# An existing token is never rotated: the Home Assistant integration holds a
# copy of it, and silently replacing it would break every camera until someone
# noticed and retyped it. An empty assignment is fixed by appending, because
# systemd lets the last assignment in the file win.
ENV_FILE="$ETC_DIR/blink-liveview-proxy.env"
if grep -qs '^BLINK_PROXY_TOKEN=..*' "$ENV_FILE"; then
  TOKEN_NOTE="kept the existing token in $ENV_FILE"
else
  ( umask 077; touch "$ENV_FILE" )
  chmod 0600 "$ENV_FILE"
  printf 'BLINK_PROXY_TOKEN=%s\n' \
    "${BLINK_PROXY_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}" \
    >> "$ENV_FILE"
  TOKEN_NOTE="wrote a new token to $ENV_FILE"
fi

install -m 0644 "$ROOT/systemd/blink-liveview-proxy.service" \
  /etc/systemd/system/blink-liveview-proxy.service

# Optional watchdog: restarts the proxy if blinkpy's token refresh gets stuck
# in a retry loop. Set INSTALL_WATCHDOG=0 to skip it.
if [ "${INSTALL_WATCHDOG:-1}" = "1" ]; then
  install -m 0755 "$ROOT/scripts/blink-liveview-proxy-watchdog.sh" \
    /usr/local/sbin/blink-liveview-proxy-watchdog.sh
  install -m 0644 "$ROOT/systemd/blink-liveview-proxy-watchdog.service" \
    /etc/systemd/system/blink-liveview-proxy-watchdog.service
  install -m 0644 "$ROOT/systemd/blink-liveview-proxy-watchdog.timer" \
    /etc/systemd/system/blink-liveview-proxy-watchdog.timer
fi

# Unattended updates: off unless asked for. This restarts the camera proxy on
# its own schedule, which is a choice to make deliberately, not a default to
# discover. INSTALL_AUTOUPDATE=1 turns it on; it stays on across re-runs.
if [ "${INSTALL_AUTOUPDATE:-0}" = "1" ]; then
  install -m 0755 "$ROOT/scripts/bootstrap.sh" \
    /usr/local/sbin/blink-liveview-proxy-update.sh
  install -m 0644 "$ROOT/systemd/blink-liveview-proxy-update.service" \
    /etc/systemd/system/blink-liveview-proxy-update.service
  install -m 0644 "$ROOT/systemd/blink-liveview-proxy-update.timer" \
    /etc/systemd/system/blink-liveview-proxy-update.timer
  # The updater has to find the checkout this install came from.
  ( umask 077; printf 'SRC_DIR=%s\n' "$ROOT" > "$ETC_DIR/update.env" )
  AUTOUPDATE_NOTE="on - daily, from the newest tag"
else
  AUTOUPDATE_NOTE="off - re-run this script, or set INSTALL_AUTOUPDATE=1"
fi

systemctl daemon-reload
systemctl enable blink-liveview-proxy.service
# restart, not start: re-running this script is also how you upgrade, and the
# running service would otherwise keep the code that was just replaced.
systemctl restart blink-liveview-proxy.service
if [ "${INSTALL_WATCHDOG:-1}" = "1" ]; then
  systemctl enable --now blink-liveview-proxy-watchdog.timer
fi
if [ "${INSTALL_AUTOUPDATE:-0}" = "1" ]; then
  systemctl enable --now blink-liveview-proxy-update.timer
fi

HEALTH_URL="http://127.0.0.1:$PROXY_PORT/health"
HEALTH_NOTE="not answering yet - check: journalctl -u blink-liveview-proxy -n 50"
for _ in $(seq 1 20); do
  if python3 -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2)" \
      "$HEALTH_URL" >/dev/null 2>&1; then
    HEALTH_NOTE="running and answering on $HEALTH_URL"
    break
  fi
  sleep 1
done

PROXY_HOST="$(hostname -f 2>/dev/null || hostname)"

cat <<MSG
Installed and started Blink Liveview Proxy.

  Service:  blink-liveview-proxy.service - $HEALTH_NOTE
  Config:   $ETC_DIR/config.json - cameras are discovered, nothing to edit
  Token:    $TOKEN_NOTE
  Auth:     $STATE_DIR/secrets/blink-auth.json - written at first Blink login
  Updates:  $AUTOUPDATE_NOTE

Nothing else to do on this host. Finish in Home Assistant:

  1. HACS -> three-dot menu -> Custom repositories, add
     https://github.com/Teethree89/ha-blink-live-view-proxy as an Integration.
     Download "Blink Liveview Proxy" and restart Home Assistant.

  2. Settings -> Devices & services -> Add integration -> Blink Liveview Proxy

       Proxy base URL:  http://$PROXY_HOST:$PROXY_PORT
       Proxy token:     sudo sed -n 's/^BLINK_PROXY_TOKEN=//p' $ENV_FILE

     The token is not printed here, so it stays out of logs and scrollback.

  3. Sidebar -> Blink Authentication -> enter your Blink email and password,
     then the PIN Blink texts you while that page waits. This is the only
     interactive step there is: Blink 2FA cannot be automated, by design.

Re-run this script to upgrade, or use the one-liner, which keeps a checkout on
this host and moves it to the newest tag:

  curl -fsSL https://raw.githubusercontent.com/Teethree89/ha-blink-live-view-proxy/main/scripts/bootstrap.sh | sudo bash

Either way your token, config, and Blink session are kept as they are.
MSG
