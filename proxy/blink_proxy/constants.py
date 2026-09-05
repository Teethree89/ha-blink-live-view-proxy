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
PROXY_VERSION = "0.7.0-rc.3"

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
    # Local clips are fetched from Blink once and kept here, with the first
    # frame of each cut as a thumbnail. Oldest files go first past the cap.
    # None means "a clips/ directory beside liveview_cache_dir": a config
    # written before this key existed then lands under the same state
    # directory as everything else, not beside the config file.
    "clip_cache_dir": None,
    "clip_cache_max_mb": 512,
    "mpegts_session_seconds": 60,
    "mpegts_cooldown_seconds": 30,
    "save_liveview_cache": True,
    "ptt_aac_bitrate": "40k",
    "ptt_strip_adts": False,
    "ptt_send_audio_config": False,
    "ptt_force_enabled_slugs": [],
    # Empty, and deliberately so. These lists used to carry "mini"/"owl",
    # from before anyone had put a Mini on the air: the family does have a
    # speaker, Blink's own app talks to it, and it has been confirmed audible
    # here — so the default was refusing a feature that works. What belongs on
    # these lists is a family that *cannot* do it, which is a property of the
    # transport rather than the model.
    "ptt_disabled_camera_types": [],
    "ptt_disabled_product_types": [],
    "prefer_v6_liveview": True,
    "send_liveview_token": True,
    "cameras": {},
}
