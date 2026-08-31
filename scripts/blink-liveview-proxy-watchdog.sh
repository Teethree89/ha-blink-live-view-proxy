#!/usr/bin/env bash
# Watches blink-liveview-proxy's journal for a stuck blinkpy token-refresh
# loop (TokenRefreshFailed / "Login endpoint failed") and restarts the
# service, which cleared the issue without any re-auth on 2026-07-27. Gives
# up after a few restarts in a row so it doesn't hammer Blink's login
# endpoint if the session genuinely needs manual re-auth (SMS 2FA).
set -euo pipefail

SERVICE="blink-liveview-proxy.service"
STATE_DIR="/var/lib/blink-liveview-proxy"
STATE_FILE="$STATE_DIR/watchdog-state"
LOOKBACK="-3 minutes"
PATTERN='TokenRefreshFailed|Login endpoint failed'

MAX_ATTEMPTS=3        # give up after this many restarts without recovery
ATTEMPT_WINDOW=1800   # ...within this many seconds (30 min)
RESTART_COOLDOWN=120  # don't restart again this soon after the last one
RECOVERY_TIME=600     # this long with no errors resets the attempt counter

mkdir -p "$STATE_DIR"
last_restart=0
attempts=0
# shellcheck disable=SC1090
[ -f "$STATE_FILE" ] && . "$STATE_FILE"

now=$(date +%s)

error_count=$(journalctl -u "$SERVICE" --since "$LOOKBACK" --no-pager -o cat 2>/dev/null | grep -Ec "$PATTERN" || true)

if [ "$error_count" -gt 0 ]; then
  since_restart=$((now - last_restart))
  if [ "$since_restart" -lt "$RESTART_COOLDOWN" ]; then
    echo "blink-liveview-proxy-watchdog: error pattern seen, restart ${since_restart}s ago is still settling; skipping"
  else
    if [ "$since_restart" -ge "$ATTEMPT_WINDOW" ]; then
      attempts=0
    fi
    if [ "$attempts" -ge "$MAX_ATTEMPTS" ]; then
      echo "blink-liveview-proxy-watchdog: $attempts restarts in the last $((ATTEMPT_WINDOW / 60)) min didn't clear it; giving up, needs manual Blink re-auth"
    else
      attempts=$((attempts + 1))
      last_restart=$now
      echo "blink-liveview-proxy-watchdog: stuck token-refresh loop detected, restarting $SERVICE (attempt $attempts/$MAX_ATTEMPTS)"
      systemctl restart "$SERVICE"
    fi
  fi
else
  if [ "$attempts" -gt 0 ] && [ $((now - last_restart)) -ge "$RECOVERY_TIME" ]; then
    echo "blink-liveview-proxy-watchdog: clean for $((RECOVERY_TIME / 60)) min, resetting attempt counter"
    attempts=0
  fi
fi

{
  echo "last_restart=$last_restart"
  echo "attempts=$attempts"
} >"$STATE_FILE"
