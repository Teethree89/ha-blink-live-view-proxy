"""Constants and defaults for the Blink live-view proxy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent.parent
LOGGER_NAME = "blink_liveview_proxy"

# Reported on /status so the Home Assistant integration can tell whether the
# proxy is old enough to be missing routes it needs. Kept in step with
# manifest.json and addon/config.yaml by a test, because a version that lies is
# worse than no version at all.
PROXY_VERSION = "0.6.1"

IMMI_HEADER_BYTES = 9

MAX_IMMI_PAYLOAD_BYTES = 1024 * 1024

IMMI_DATA_FLAG_AUDIO = 0x05

IMMI_DATA_FLAG_AUDIO_CONFIG = 0x0C

IMMI_DATA_FLAG_SESSION_LV_CMD = 0x17

IMMI_AUDIO_CONFIG_SEQUENCE = 0xA0000001

LIVEVIEW_SESSION_COMMAND_START_AUDIO = 3

LIVEVIEW_SESSION_COMMAND_STOP_AUDIO = 4

AUDIO_CLOCK_RATE = 90_000

AAC_FRAME_SAMPLES = 1024

PTT_TARGET_SAMPLE_RATE = 16_000

DEFAULT_CONFIG: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8088,
    "auth_file": "secrets/blink-auth.json",
    "username_env": "BLINK_USERNAME",
    "password_env": "BLINK_PASSWORD",
    "twofa_env": "BLINK_2FA_CODE",
    "proxy_token_env": "BLINK_PROXY_TOKEN",
    "ffmpeg": "ffmpeg",
    "ffmpeg_loglevel": "warning",
    "ffmpeg_probesize": 1_000_000,
    "ffmpeg_analyzeduration": 500_000,
    "hls_dir": ".runtime/blink-liveview-proxy",
    "hls_idle_timeout": 10,
    "hls_start_timeout": 30,
    "hls_transcode": False,
    "hls_frame_rate": 24,
    "liveview_cache_dir": ".runtime/blink-liveview-proxy/liveviews",
    "mpegts_session_seconds": 60,
    "mpegts_cooldown_seconds": 30,
    "save_liveview_cache": True,
    "ptt_aac_bitrate": "40k",
    "ptt_strip_adts": False,
    "ptt_send_audio_config": False,
    "ptt_force_enabled_slugs": [],
    "ptt_disabled_camera_types": ["mini"],
    "ptt_disabled_product_types": ["owl"],
    "prefer_v6_liveview": True,
    "send_liveview_token": True,
    "cameras": {},
}
