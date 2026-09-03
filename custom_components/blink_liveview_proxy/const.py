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
# The oldest proxy this integration can drive without hitting routes that
# build never had. Raise it only when something here genuinely requires a newer
# proxy — every bump puts a repair notice in front of users who are otherwise
# working fine.
MINIMUM_PROXY_VERSION = "0.3.0"
# The proxy release that first reports its environment on /status. An older
# proxy is not broken by this; the dashboard simply cannot ask it what blinkpy
# and ffmpeg it has, and says so rather than guessing.
ENVIRONMENT_PROXY_VERSION = "0.6.1"
# The blinkpy pin from proxy/requirements.txt, repeated here so the dashboard
# can say whether the proxy is running the version this release was tested
# against. A test keeps the two numbers in step - see tests/test_assets.py.
REQUIRED_BLINKPY_VERSION = "0.25.9"
# The Home Assistant floor, the same number hacs.json enforces at install time.
MINIMUM_HA_VERSION = "2024.6.0"
REPOSITORY_URL = "https://github.com/Teethree89/ha-blink-live-view-proxy"

# Where this integration's own frontend files are served from, and why the path
# does not say "static".
#
# Home Assistant's service worker registers, before its /api rule and therefore
# ahead of it, a CacheFirst route for /(static|frontend_latest|frontend_es5)/.+
# — and Workbox matches a RegExp anywhere in a same-origin URL, not only at the
# start. A path containing /static/ anywhere therefore matched, so the browser
# served these files from Cache Storage without ever asking the server again.
# That route also sets ignoreSearch, so a ?v= cache-buster is stripped from the
# key and changes nothing. Only the path itself can move.
#
# It only ever bit HTTPS, because a service worker needs a secure context —
# which is exactly how it stayed hidden: over plain HTTP everything was fresh.
ASSET_URL_BASE = "/api/blink_liveview_proxy/assets"
# The pre-0.6.2 path, still served. Dashboards, YAML configs and hand-written
# resource lists in the wild point at it, and a 404 there is the silent failure
# this whole area exists to avoid.
LEGACY_ASSET_URL_BASE = "/api/blink_liveview_proxy/static"

# The dashboard helper module. Every live view, clips and snapshot button in a
# generated dashboard fires an event this resource listens for, so without it
# they are silently inert. __init__.py registers it and the panel reports it.
FRONTEND_RESOURCE_URL = f"{ASSET_URL_BASE}/blink-liveview-dialog.js"
LEGACY_FRONTEND_RESOURCE_URL = f"{LEGACY_ASSET_URL_BASE}/blink-liveview-dialog.js"
DEFAULT_SCAN_INTERVAL = timedelta(seconds=30)
DEFAULT_STREAM_SECONDS = 60

PLATFORMS = [Platform.CAMERA, Platform.BINARY_SENSOR]
