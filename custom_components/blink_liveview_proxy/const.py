"""Constants for the Blink live-view proxy integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "blink_liveview_proxy"

CONF_BASE_URL = "base_url"
CONF_STREAM_SECONDS = "stream_seconds"
CONF_TOKEN = "token"

DEFAULT_BASE_URL = "http://127.0.0.1:8088"
# Where the add-on leaves its generated proxy token, and the URL that reaches
# an add-on from inside Home Assistant. Both only apply to the add-on install.
TOKEN_HANDOFF_FILE = "blink_liveview_proxy.token"
ADDON_BASE_URL = "http://homeassistant.local:8088"
DEFAULT_SCAN_INTERVAL = timedelta(seconds=30)
DEFAULT_STREAM_SECONDS = 60

PLATFORMS = [Platform.CAMERA, Platform.BINARY_SENSOR]
