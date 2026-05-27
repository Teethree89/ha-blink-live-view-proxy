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
        # No pin yet — initiate login so Blink sends the 2FA SMS/email,
        # then exit cleanly and tell the user what to do next.
        bashio::log.info "No auth file found. Sending credentials to Blink to trigger 2FA..."
        "$PYTHON" /opt/proxy/blink_liveview_proxy.py \
            --config "$CONFIG_FILE" list 2>/dev/null || true
        bashio::log.warning "---------------------------------------------------------------"
        bashio::log.warning "Blink sent a 2FA PIN to your registered phone or email."
        bashio::log.warning "1. Copy the PIN from your phone/email."
        bashio::log.warning "2. Open the add-on Configuration tab."
        bashio::log.warning "3. Paste the PIN into blink_2fa_code and save."
        bashio::log.warning "4. Restart the add-on."
        bashio::log.warning "The PIN expires in a few minutes — restart promptly."
        bashio::log.warning "---------------------------------------------------------------"
        exit 0
    else
        bashio::log.info "No auth file — completing Blink 2FA login..."
        PIN="$(bashio::config 'blink_2fa_code')"
        export BLINK_2FA_CODE="$PIN"
        if "$PYTHON" /opt/proxy/blink_liveview_proxy.py \
            --config "$CONFIG_FILE" --pin "$PIN" list; then
            bashio::log.info "Authentication succeeded."
            bashio::log.info "You may clear blink_2fa_code from the add-on options."
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
