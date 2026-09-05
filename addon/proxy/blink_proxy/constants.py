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
    # "xt" and "white" are the two families Blink hands an rtsps:// URL rather
    # than immis://. Push-to-talk goes through send_session_command(), which
    # exists only on the IMMI path - BlinkRtspLiveStream raises
    # NotImplementedError for it, deliberately, because RTSP has no equivalent.
    # Left off this list the button is offered, is pressable, and can only fail.
    #
    # product_type rather than camera_type, and that is the whole point of the
    # entry: an xt reports camera_type "default", and so does a catalina, which
    # does have push-to-talk over IMMI. Gating on camera_type would take the
    # feature away from a family that has it.
    #
    # Measured 2026-09-04 on an xt: a handshake against /cameras/<slug>/ptt
    # upgraded with 101, exactly like the catalina and lotus beside it, and the
    # refusal then landed on the NotImplementedError at the end of the chain.
    # "white" is @bbolinger's measurement rather than mine; there is no white
    # on the account this was measured on.
    #
    # "superior" is on this list for a DIFFERENT reason, and the distinction
    # matters to whoever reads it next. xt and white are "never": the transport
    # has no way to carry it. superior is "not yet": it gets immis://, the path
    # exists, and it should work. It does not, because the audio shape the
    # camera expects is not the one we send.
    #
    # @bbolinger measured the cost on a Wired Floodlight: with the proxy's
    # audio config on, the camera closes the stream about four seconds into the
    # hold; without it, the close comes a few seconds after release. Either way
    # it then refuses to rejoin for about three minutes. So the button does not
    # merely fail there - it costs the live view.
    #
    # WHAT WOULD TAKE IT BACK OFF: a capture of what Blink's own app sends to a
    # superior. Match that shape and the entry is obsolete; remove it then.
    # Until anyone has one, ptt_force_enabled_slugs keeps the door open per
    # camera for whoever wants to try.
    "ptt_disabled_product_types": ["xt", "white", "superior"],
    "prefer_v6_liveview": True,
    "send_liveview_token": True,
    "cameras": {},
}
