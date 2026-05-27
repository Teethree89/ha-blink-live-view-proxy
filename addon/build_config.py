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
    "liveview_cache_dir": "/data/liveviews",
    "cameras": cameras,
}

json.dump(config, sys.stdout, indent=2)
print()
