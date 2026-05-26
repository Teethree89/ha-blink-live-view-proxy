#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPT_DIR="${OPT_DIR:-/opt/blink-liveview-proxy}"
ETC_DIR="${ETC_DIR:-/etc/blink-liveview-proxy}"
STATE_DIR="${STATE_DIR:-/var/lib/blink-liveview-proxy}"

install -d "$OPT_DIR" "$ETC_DIR" "$STATE_DIR/secrets"
install -m 0644 "$ROOT/proxy/blink_liveview_proxy.py" "$OPT_DIR/blink_liveview_proxy.py"
install -m 0644 "$ROOT/proxy/requirements.txt" "$OPT_DIR/requirements.txt"

python3 -m venv "$OPT_DIR/.venv"
"$OPT_DIR/.venv/bin/python" -m pip install --upgrade pip
"$OPT_DIR/.venv/bin/python" -m pip install -r "$OPT_DIR/requirements.txt"

if [ ! -f "$ETC_DIR/config.json" ]; then
  install -m 0600 "$ROOT/proxy/config.example.json" "$ETC_DIR/config.json"
fi

install -m 0644 "$ROOT/systemd/blink-liveview-proxy.service" \
  /etc/systemd/system/blink-liveview-proxy.service

systemctl daemon-reload
systemctl enable blink-liveview-proxy.service

cat <<MSG
Installed Blink Liveview Proxy.

Next:
  1. Edit $ETC_DIR/config.json
  2. Run first Blink login:
     BLINK_USERNAME='you@example.com' BLINK_PASSWORD='...' BLINK_2FA_CODE='123456' \\
       $OPT_DIR/.venv/bin/python $OPT_DIR/blink_liveview_proxy.py --config $ETC_DIR/config.json list
  3. Start service:
     systemctl start blink-liveview-proxy.service
MSG
