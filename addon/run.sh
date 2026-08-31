#!/usr/bin/with-contenv bashio

CONFIG_FILE=/data/config.json
AUTH_FILE=/data/blink-auth.json
PYTHON=/opt/venv/bin/python

bashio::log.info "Building proxy configuration..."
"$PYTHON" /opt/build_config.py > "$CONFIG_FILE"

BLINK_USERNAME="$(bashio::config 'blink_username')"
BLINK_PASSWORD="$(bashio::config 'blink_password')"
export BLINK_USERNAME BLINK_PASSWORD

# No "list" pre-run any more.
#
# It used to log in once for "list" and then again for "serve", two logins per
# start. Blink throttles repeated logins hard - five attempts in 55 seconds and
# every later one comes back "Login failed", even with correct credentials.
#
# Since the 2FA code is now redeemed inside the serving process (see
# blink_proxy/blink.py), the pre-run is worse than wasteful: it would open the
# OAuth session, wait for the code, and then "serve" would open a second,
# entirely new session with a different hardware_id - which is the bug the code
# is tied to.
#
# So: one invocation. "serve" logs in itself and waits for the code if needed.

if [ ! -f "$AUTH_FILE" ]; then
    bashio::log.info "No auth file yet - the proxy will log in and, if Blink"
    bashio::log.info "asks for a 2FA code, wait for it in the same session."
fi

# blink_2fa_code is deliberately NOT exported.
#
# A code sitting in the option at start time is always from a previous
# challenge: Blink only issues one after a sign-in has begun, and nothing
# clears the option once a code has been used. Exporting it makes `code`
# truthy in BlinkClient.start(), which skips _wait_for_pin() entirely, hands
# the stale code to send_2fa_code() and fails the login. The user is then
# stuck until they empty the field by hand, because the new code they were
# just texted never gets asked for.
#
# The proxy reads the option live from the Supervisor while it waits, so a
# code typed during the wait is picked up without this.

PORT="$(bashio::config 'port')"
bashio::log.info "Starting Blink Liveview Proxy on port ${PORT}..."
exec "$PYTHON" /opt/proxy/blink_liveview_proxy.py \
    --config "$CONFIG_FILE" \
    serve --host "0.0.0.0" --port "$PORT"
