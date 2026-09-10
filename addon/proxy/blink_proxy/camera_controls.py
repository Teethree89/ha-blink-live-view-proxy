"""Per-camera controls for the live-view player: lamp, night vision, volume.

Blink keeps these behind two different config endpoints. The Wired Floodlight
(product type ``superior``) is an "owl" and answers the owl config route,
with the lamp settings nested under a ``superior`` block. Every other camera
answers the classic ``/network/{n}/camera/{id}/config`` route, where the same
knobs use integer codes. This module hides that split behind one flat state
document so the player never has to know which kind of camera it is looking at.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from blinkpy import api as blink_api

LOGGER = logging.getLogger(__name__)

FLOODLIGHT_TYPES = {"superior"}
# Cameras whose speaker volume the app keeps in the classic config as
# lfr_sync_interval (found by watching the app save its Speaker Volume slider).
SPEAKER_TYPES = {"catalina", "xt2"}
NIGHT_VISION_CODES = {0: "off", 1: "on", 2: "auto"}
NIGHT_VISION_WORDS = {"off", "on", "auto"}
LAMP_BRIGHTNESS_RANGE = (1, 10)
VOLUME_RANGE = (1, 8)


def _product_type(row: dict[str, Any]) -> str:
    return str(row.get("product_type") or "").casefold()


def capabilities(product_type: str) -> dict[str, bool]:
    """Say which controls a camera of this product type can offer."""
    floodlight = product_type.casefold() in FLOODLIGHT_TYPES
    return {
        "flood_light": floodlight,
        "night_vision": True,
        "brightness": floodlight,
        # The floodlight has no thermometer; every battery and plug-in camera does.
        "temperature": not floodlight,
        "volume": floodlight or product_type.casefold() in SPEAKER_TYPES,
        "light_settings": floodlight,
    }


def _clamp(value: Any, low: int, high: int) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(low, min(high, number))


def _night_vision_word(raw: Any) -> str | None:
    if isinstance(raw, str) and raw.casefold() in NIGHT_VISION_WORDS:
        return raw.casefold()
    if isinstance(raw, int) and raw in NIGHT_VISION_CODES:
        return NIGHT_VISION_CODES[raw]
    return None


def _standard_camera(config: Any) -> dict[str, Any]:
    """Unwrap the classic config document down to its camera block."""
    if not isinstance(config, dict):
        return {}
    camera = config.get("camera", config)
    if isinstance(camera, list):
        camera = camera[0] if camera else {}
    return camera if isinstance(camera, dict) else {}


def read_state(
    row: dict[str, Any], config: Any, owl_config: Any
) -> dict[str, Any]:
    """Fold whichever config document a camera answered into one flat state."""
    product_type = _product_type(row)
    caps = capabilities(product_type)
    state: dict[str, Any] = {
        "slug": row.get("slug"),
        "name": row.get("name"),
        "product_type": product_type,
        "capabilities": caps,
        "flood_light": None,
        "night_vision": None,
        "brightness": None,
        "volume": None,
        "temperature_f": None,
        "light_settings": None,
    }
    if caps["flood_light"] and isinstance(owl_config, dict):
        lamp = owl_config.get("superior")
        lamp = lamp if isinstance(lamp, dict) else {}
        state["flood_light"] = str(owl_config.get("light_status") or "").casefold() == "on"
        state["night_vision"] = _night_vision_word(owl_config.get("illuminator_enable"))
        state["brightness"] = _clamp(
            lamp.get("illuminator_intensity", owl_config.get("light_brightness")),
            *LAMP_BRIGHTNESS_RANGE,
        )
        state["volume"] = _clamp(owl_config.get("volume_control"), *VOLUME_RANGE)
        state["light_settings"] = {
            "dusk_to_dawn": bool(lamp.get("auto_on_off_enabled")),
            "motion_activation": bool(lamp.get("motion_alert")),
            "motion_duration": lamp.get("illuminator_duration"),
            "manual_duration": lamp.get("manual_illuminator_duration"),
            "motion_duration_options": lamp.get("illuminator_duration_options") or [],
            "manual_duration_options": lamp.get("manual_illuminator_duration_options")
            or [],
        }
        return state

    camera = _standard_camera(config)
    state["night_vision"] = _night_vision_word(camera.get("illuminator_enable"))
    if caps["volume"]:
        state["volume"] = _clamp(camera.get("lfr_sync_interval"), *VOLUME_RANGE)
    signals = config.get("signals") if isinstance(config, dict) else None
    temperature = None
    if isinstance(signals, dict):
        temperature = signals.get("temp")
    if temperature is None:
        temperature = camera.get("temperature")
    try:
        state["temperature_f"] = int(round(float(temperature)))
    except (TypeError, ValueError):
        state["temperature_f"] = None
    return state


class ControlError(ValueError):
    """A request asked for something this camera cannot do."""


def validate_changes(row: dict[str, Any], changes: Any) -> dict[str, Any]:
    """Keep only known fields with in-range values, or explain what is wrong."""
    if not isinstance(changes, dict) or not changes:
        raise ControlError("Body must be a JSON object with at least one control")
    caps = capabilities(_product_type(row))
    clean: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "flood_light":
            if not caps["flood_light"]:
                raise ControlError("This camera has no flood light")
            if not isinstance(value, bool):
                raise ControlError("flood_light must be true or false")
            clean[key] = value
        elif key == "night_vision":
            word = _night_vision_word(value)
            if word is None:
                raise ControlError("night_vision must be auto, on, or off")
            clean[key] = word
        elif key == "brightness":
            if not caps["brightness"]:
                raise ControlError("This camera has no lamp brightness")
            number = _clamp(value, *LAMP_BRIGHTNESS_RANGE)
            if number is None or number != value:
                raise ControlError("brightness must be a whole number from 1 to 10")
            clean[key] = number
        elif key == "volume":
            if not caps["volume"]:
                raise ControlError("This camera has no volume control")
            number = _clamp(value, *VOLUME_RANGE)
            if number is None or number != value:
                raise ControlError("volume must be a whole number from 1 to 8")
            clean[key] = number
        elif key == "light_settings":
            if not caps["light_settings"]:
                raise ControlError("This camera has no light settings")
            clean[key] = _validate_light_settings(value)
        else:
            raise ControlError(f"Unknown control: {key}")
    return clean


def _validate_light_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ControlError("light_settings must be a JSON object")
    clean: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"dusk_to_dawn", "motion_activation"}:
            if not isinstance(item, bool):
                raise ControlError(f"{key} must be true or false")
            clean[key] = item
        elif key in {"motion_duration", "manual_duration"}:
            if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
                raise ControlError(f"{key} must be a positive number of seconds")
            clean[key] = item
        else:
            raise ControlError(f"Unknown light setting: {key}")
    return clean


LIGHT_SETTING_FIELDS = {
    "dusk_to_dawn": "auto_on_off_enabled",
    "motion_activation": "motion_alert",
    "motion_duration": "illuminator_duration",
    "manual_duration": "manual_illuminator_duration",
}


def write_plan(row: dict[str, Any], clean: dict[str, Any]) -> list[tuple[str, Any]]:
    """Turn validated changes into the Blink calls that carry them.

    Returns ``(kind, payload)`` pairs: ``lights`` flips the floodlight lamp
    through its own on/off route, ``owl_config`` posts a JSON document to the
    owl config route, ``camera_update`` posts one to the classic camera
    update route, and ``camera_config_v2`` posts one to the v2 camera config
    route the app uses for its Speaker Volume slider.
    """
    floodlight = _product_type(row) in FLOODLIGHT_TYPES
    plan: list[tuple[str, Any]] = []
    owl: dict[str, Any] = {}
    classic: dict[str, Any] = {}
    if "flood_light" in clean:
        plan.append(("lights", clean["flood_light"]))
    if "night_vision" in clean:
        word = clean["night_vision"]
        if floodlight:
            owl["illuminator_enable"] = word
        else:
            classic["illuminator_enable"] = {"off": 0, "on": 1, "auto": 2}[word]
    if "brightness" in clean:
        owl.setdefault("superior", {})["illuminator_intensity"] = clean["brightness"]
        owl["light_brightness"] = clean["brightness"]
    if "volume" in clean:
        if floodlight:
            owl["volume_control"] = clean["volume"]
        else:
            plan.append(("camera_config_v2", {"lfr_sync_interval": clean["volume"]}))
    for key, value in clean.get("light_settings", {}).items():
        owl.setdefault("superior", {})[LIGHT_SETTING_FIELDS[key]] = value
    if owl:
        plan.append(("owl_config", owl))
    if classic:
        plan.append(("camera_update", classic))
    return plan


async def fetch_state(blink: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Read the camera's current settings from Blink."""
    network = row["network_id"]
    camera_id = row["id"]
    config = None
    owl_config = None
    if _product_type(row) in FLOODLIGHT_TYPES:
        owl_config = await blink_api.request_get_config(
            blink, network, camera_id, product_type="owl"
        )
    else:
        config = await blink_api.request_camera_info(blink, network, camera_id)
    return read_state(row, config, owl_config)


def _answer_accepted(answer: Any) -> bool:
    """Blink answers 200 to everything; only the body says whether it took."""
    if not isinstance(answer, dict):
        return False
    if answer.get("code") == 307:
        return False
    return answer.get("command") is not None or answer.get("state") == "new"


async def apply_changes(
    blink: Any, row: dict[str, Any], changes: Any
) -> dict[str, Any]:
    """Send the requested changes to Blink and return what it holds now."""
    clean = validate_changes(row, changes)
    network = row["network_id"]
    camera_id = row["id"]
    rejected: list[str] = []
    for kind, payload in write_plan(row, clean):
        if kind == "lights":
            answer = await blink_api.request_floodlight(
                blink, network, camera_id, payload
            )
            if isinstance(answer, dict) and answer.get("code") == 307:
                rejected.append("flood_light")
            continue
        if kind == "owl_config":
            url = (
                f"{blink.urls.base_url}/api/v1/accounts/{blink.account_id}"
                f"/networks/{network}/owls/{camera_id}/config"
            )
        elif kind == "camera_config_v2":
            url = (
                f"{blink.urls.base_url}/api/v2/accounts/{blink.account_id}"
                f"/networks/{network}/cameras/{camera_id}/config"
            )
        else:
            url = f"{blink.urls.base_url}/network/{network}/camera/{camera_id}/update"
        # blinkpy documents the body as a JSON string. A dict is sent
        # form-encoded and Blink answers 200 while ignoring it.
        response = await blink_api.http_post(
            blink, url, json=False, data=json.dumps(payload)
        )
        answer: Any = None
        try:
            answer = await response.json(content_type=None)
        except Exception:  # noqa: BLE001
            answer = None
        if not _answer_accepted(answer):
            LOGGER.warning(
                "Blink did not accept %s for %s: %s", kind, row.get("slug"), answer
            )
            rejected.extend(sorted(payload))
    state = await fetch_state(blink, row)
    state["rejected"] = rejected
    return state
