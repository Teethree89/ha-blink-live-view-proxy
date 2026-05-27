#!/usr/bin/with-contenv bashio

CONFIG_FILE=/data/config.json
AUTH_FILE=/data/blink-auth.json
PYTHON=/opt/venv/bin/python

bashio::log.info "Building proxy configuration..."
"$PYTHON" /opt/build_config.py > "$CONFIG_FILE"

BLINK_USERNAME="$(bashio::config 'blink_username')"
BLINK_PASSWORD="$(bashio::config 'blink_password')"
export BLINK_USERNAME BLINK_PASSWORD

if [ ! -f "$AUTH_FILE" ]; then
    if bashio::config.is_empty 'blink_2fa_code'; then
        bashio::log.warning "No Blink auth file found and blink_2fa_code is not set."
        bashio::log.warning "Set blink_2fa_code in add-on options and restart to authenticate."
    else
        bashio::log.info "No auth file — running first-time Blink login..."
        PIN="$(bashio::config 'blink_2fa_code')"
        export BLINK_2FA_CODE="$PIN"
        if "$PYTHON" /opt/proxy/blink_liveview_proxy.py \
            --config "$CONFIG_FILE" --pin "$PIN" list; then
            bashio::log.info "Authentication succeeded. You may clear blink_2fa_code from options."
        else
            bashio::log.fatal "Authentication failed. Check blink_username, blink_password, and blink_2fa_code."
        fi
    fi
fi

PORT="$(bashio::config 'port')"
bashio::log.info "Starting Blink Liveview Proxy on port ${PORT}..."
exec "$PYTHON" /opt/proxy/blink_liveview_proxy.py \
    --config "$CONFIG_FILE" \
    serve --host "0.0.0.0" --port "$PORT"
