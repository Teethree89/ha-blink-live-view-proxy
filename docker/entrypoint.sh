#!/usr/bin/env bash
# Configure from the environment on first start, then serve.
#
# Same provisioning promises as the installer and the add-on: a config that
# discovers cameras, and an API token that exists whether or not anyone thought
# to set one. Nothing here writes a credential to the log.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
CONFIG_FILE="${BLINK_PROXY_CONFIG:-$DATA_DIR/config.json}"
TOKEN_FILE="$DATA_DIR/proxy-token"
PORT="${PROXY_PORT:-8088}"

mkdir -p "$DATA_DIR"

# Only defaults that differ from the built-in ones are written, so upgrades
# keep inheriting new defaults instead of freezing this file's idea of them.
if [ ! -f "$CONFIG_FILE" ]; then
  python3 - "$CONFIG_FILE" "$DATA_DIR" "$PORT" <<'PY'
import json
import sys

path, data, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
config = {
    "host": "0.0.0.0",
    "port": port,
    "auth_file": f"{data}/blink-auth.json",
    "hls_dir": f"{data}/hls",
    "liveview_cache_dir": f"{data}/liveviews",
    "clip_cache_dir": f"{data}/clips",
    "cameras": {},
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
    handle.write("\n")
PY
  echo "Wrote a starting config to $CONFIG_FILE (cameras are discovered)."
fi

# Browser authentication is refused without a token, so provision one rather
# than shipping an image whose panel cannot work.
if [ -z "${BLINK_PROXY_TOKEN:-}" ]; then
  if [ ! -s "$TOKEN_FILE" ]; then
    ( umask 077
      python3 -c 'import secrets; print(secrets.token_hex(32))' > "$TOKEN_FILE" )
    echo "Generated a proxy API token."
  fi
  BLINK_PROXY_TOKEN="$(cat "$TOKEN_FILE")"
  export BLINK_PROXY_TOKEN
  echo "Proxy API token is in $TOKEN_FILE inside this container."
  echo "Read it with: docker exec <container> cat $TOKEN_FILE"
fi

exec python3 /opt/proxy/blink_liveview_proxy.py --config "$CONFIG_FILE" serve
