#!/usr/bin/env python3
"""Generate proxy config.json from Home Assistant add-on options (/data/options.json)."""

import json
import sys

with open("/data/options.json") as f:
    options = json.load(f)

cameras: dict = {}
for cam in options.get("cameras", []):
    cam = {k: v for k, v in cam.items() if v is not None}
    slug = cam.pop("slug")
    cameras[slug] = cam

config = {
    "host": "0.0.0.0",
    "port": options.get("port", 8088),
    "auth_file": "/data/blink-auth.json",
    "username_env": "BLINK_USERNAME",
    "password_env": "BLINK_PASSWORD",
    "twofa_env": "BLINK_2FA_CODE",
    "proxy_token_env": "BLINK_PROXY_TOKEN",
    "ffmpeg": "ffmpeg",
    "hls_dir": "/data/hls",
    "hls_transcode": bool(options.get("low_latency", False)),
    "liveview_cache_dir": "/data/liveviews",
    "clip_cache_dir": "/data/clips",
    "cameras": cameras,
}

# Push-to-talk gating, forwarded only when it was actually set.
#
# An empty list here means "the add-on user did not say", not "allow
# everything": writing [] through would override the proxy's own defaults and
# offer Hold Talk on camera families where it cannot work. Blank boxes are the
# state every add-on starts in, so this is the difference between exposing the
# option and breaking the default.
for key in (
    "ptt_disabled_product_types",
    "ptt_disabled_camera_types",
    "ptt_force_enabled_slugs",
):
    value = options.get(key) or []
    if value:
        config[key] = value

json.dump(config, sys.stdout, indent=2)
print()
