#!/usr/bin/env bash
# Actual first-start/recreation with an empty volume. Network isolation prevents
# any Blink request. Auth-file persistence uses a fixture, not a real session.
set -euo pipefail
image="${1:?image name required}"
container="blink-smoke-${RANDOM}-$$"
volume="${container}-data"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker volume create "$volume" >/dev/null
start() {
  docker run -d --network none --name "$container" -v "$volume:/data" "$image" >/dev/null
  for attempt in $(seq 1 60); do
    if docker exec "$container" python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8088/health", timeout=1)' >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  echo "Proxy did not become healthy" >&2
  return 1
}
fingerprint() {
  docker exec "$container" python3 -c 'from pathlib import Path; import hashlib; print(hashlib.sha256(b"".join(Path(p).read_bytes() for p in ("/data/config.json", "/data/proxy-token", "/data/blink-auth.json"))).hexdigest())'
}
start
docker exec "$container" python3 -c 'from pathlib import Path; import json; config=json.loads(Path("/data/config.json").read_text()); assert config["cameras"] == {}; assert len(Path("/data/proxy-token").read_text().strip()) == 64; Path("/data/blink-auth.json").write_text("{}")'
before="$(fingerprint)"
docker rm -f "$container" >/dev/null
start
after="$(fingerprint)"
test "$before" = "$after"
echo "PASS: empty-volume startup, health, generated config/token, recreation preserves config/token/auth fixture"
