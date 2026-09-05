#!/usr/bin/with-contenv bashio

CONFIG_FILE=/data/config.json
AUTH_FILE=/data/blink-auth.json
PYTHON=/opt/venv/bin/python

bashio::log.info "Building proxy configuration..."
"$PYTHON" /opt/build_config.py > "$CONFIG_FILE"

BLINK_USERNAME="$(bashio::config 'blink_username')"
BLINK_PASSWORD="$(bashio::config 'blink_password')"

# Proxy API token, provisioned rather than requested.
#
# The browser authentication routes refuse to run without one, and inventing a
# token by hand is exactly the setup step people skip, so generate one on first
# start and keep it in /data. An explicit proxy_api_token option always wins,
# and the generated value survives restarts and add-on updates.
TOKEN_FILE=/data/proxy-token
if bashio::config.has_value 'proxy_api_token'; then
    BLINK_PROXY_TOKEN="$(bashio::config 'proxy_api_token')"
else
    if [ ! -s "$TOKEN_FILE" ]; then
        ( umask 077
          "$PYTHON" -c 'import secrets; print(secrets.token_hex(32))' > "$TOKEN_FILE" )
        bashio::log.info "Generated a proxy API token (kept in the add-on's /data)."
    fi
    BLINK_PROXY_TOKEN="$(cat "$TOKEN_FILE")"
fi
export BLINK_USERNAME BLINK_PASSWORD BLINK_PROXY_TOKEN

PORT="$(bashio::config 'port')"

# Hand the token and the address to the Home Assistant integration, so nobody
# has to copy either. Its config flow reads both out of the Home Assistant
# config directory and pre-fills the form. Rewritten every start, so changing
# an option heals the integration on the next restart. The token is never
# logged: add-on logs get pasted into issues.
#
# The address is the one Supervisor gave this container — its own hostname on
# Home Assistant's network — and the port this start is actually listening on.
# Not homeassistant.local: the add-on publishes no host port unless someone
# maps one by hand, so that name reached nothing on a stock install and setup
# failed with "cannot connect" while the proxy sat here working.
ADDON_HOST="$(bashio::addon.hostname 2>/dev/null)" || ADDON_HOST=""
if [ -z "$ADDON_HOST" ]; then
    # Supervisor sets the container hostname to exactly that name, so this
    # fallback is the same answer from the other direction.
    ADDON_HOST="$(hostname 2>/dev/null)" || ADDON_HOST=""
fi

for HA_CONFIG in /homeassistant /config; do
    if [ -d "$HA_CONFIG" ] && [ -w "$HA_CONFIG" ]; then
        ( umask 077
          printf '%s\n' "$BLINK_PROXY_TOKEN" > "$HA_CONFIG/blink_liveview_proxy.token" )
        bashio::log.info "Shared the proxy token with Home Assistant."
        if [ -n "$ADDON_HOST" ]; then
            printf 'http://%s:%s\n' "$ADDON_HOST" "$PORT" \
                > "$HA_CONFIG/blink_liveview_proxy.url"
            bashio::log.info "Home Assistant will reach the proxy at http://${ADDON_HOST}:${PORT}."
        fi
        break
    fi
done

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

bashio::log.info "Starting Blink Live View Proxy on port ${PORT}..."
exec "$PYTHON" /opt/proxy/blink_liveview_proxy.py \
    --config "$CONFIG_FILE" \
    serve --host "0.0.0.0" --port "$PORT"
