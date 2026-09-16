"""Per-camera controls for the live-view player: lamp, night vision, volume.

Blink keeps these behind three different config endpoints. The Wired Floodlight
(product type ``superior``) is addressed under Blink's ``/owls/`` path, with its
lamp settings nested in a ``superior`` block; that path is Blink's own grouping
and covers more than the Blink Mini, which is the camera blinkpy calls an owl.
Every other camera answers the classic ``/network/{n}/camera/{id}/update``
route, where the same knobs use integer codes. Speaker volume is the exception:
the app saves it to a v2 camera config route that blinkpy does not carry, as
``lfr_sync_interval``. This module hides that split behind one flat state
document so the player never has to know which kind of camera it is looking at.

``request_update_config`` chooses its route from ``product_type`` and only
recognises ``owl`` and ``catalina``; handed anything else it logs "config update
not implemented" and returns None without making a request. That covers exactly
one of the cameras here, the catalina. A superior, xt, xt2, white or lotus gets
nothing, which is why blinkpy's own ``async_set_night_vision`` is a no-op on
them and why these routes were originally built by hand.

So the word handed to it below is the name of a route, not a description of the
camera: ``owl`` for the ``/owls/`` config route the floodlight answers, and
``catalina`` for the classic update route that catalina, xt, xt2 and white all
answer. The request is blinkpy's, the choice of which one is this module's, and
that distinction is worth keeping visible rather than reading as though blinkpy
supports these cameras.

Only the v2 speaker volume route has no blinkpy function at all.

The lamp is the other exception, for a different reason. blinkpy's
``request_floodlight`` builds the right route, but Blink answers it 307 "system
is busy" for as long as any live view is open on the camera, and the Controls
sheet exists only inside one. The Blink app never contends with that: while it
is watching, it sends the lamp as an inline command over the live-view session
itself. So, when the proxy holds a live view on the camera, the lamp goes over
that session; the route is kept for a write that arrives with nothing streaming.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from typing import Any

from blinkpy import api as blink_api

LOGGER = logging.getLogger(__name__)

FLOODLIGHT_TYPES = {"superior"}
# Cameras whose speaker volume the app saves to the v2 config as
# lfr_sync_interval: their Audio screen has one Volume slider, saving it writes
# this field, a level written here moves that slider to match, and the speaker
# is audibly louder or quieter for it. The camera reads it at session setup, so
# a change lands on the next stream rather than the one playing.
SPEAKER_TYPES = {"catalina", "xt2"}
NIGHT_VISION_CODES = {0: "off", 1: "on", 2: "auto"}
NIGHT_VISION_WORDS = {"off", "on", "auto"}
LAMP_BRIGHTNESS_RANGE = (1, 10)
VOLUME_RANGE = (1, 8)
# The app's own writes finished in about six seconds; blinkpy would wait two
# minutes, which is far longer than the sheet can hold a request open.
COMMAND_WAIT_SECONDS = 12
READ_BACK_INTERVAL = 1.5
# A held-back write goes out once the last live view on the camera has closed.
# Blink released the camera within a second of the proxy's session ending
# when measured; the settle time is margin on that. While the Blink app is
# still streaming the route stays busy, so the flush retries for a while and
# then gives up and says so.
DEFERRED_SETTLE_SECONDS = 2.0
DEFERRED_RETRY_SECONDS = 5.0
DEFERRED_GIVE_UP_SECONDS = 120.0


def _product_type(row: dict[str, Any]) -> str:
    return str(row.get("product_type") or "").casefold()


def control_names(changes: dict[str, Any]) -> list[str]:
    """Name the controls in a changes document, light settings by their own names."""
    names: list[str] = []
    for key, value in changes.items():
        if key == "light_settings" and isinstance(value, dict):
            names.extend(value.keys())
        else:
            names.append(key)
    return names


def _subset(changes: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """The part of a changes document that carries the named controls."""
    wanted = set(names)
    out: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "light_settings" and isinstance(value, dict):
            kept = {name: item for name, item in value.items() if name in wanted}
            if kept:
                out["light_settings"] = kept
        elif key in wanted:
            out[key] = value
    return out


def _overlay(state: dict[str, Any], changes: dict[str, Any]) -> None:
    """Show a changes document on top of a state document."""
    for key, value in changes.items():
        if key == "light_settings" and isinstance(value, dict):
            if isinstance(state.get("light_settings"), dict):
                state["light_settings"].update(value)
        else:
            state[key] = value


def now_stamp() -> str:
    """The moment a held-back write was carried out, for the sheet to show."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class DeferredControls:
    """Settings held back while a live view keeps Blink busy, written when it ends.

    Blink answers the floodlight's config route 307 for as long as any live
    view is open on the camera, and the Controls sheet only exists inside one,
    so a change made there can never land while it is made. It lands a second
    after the session closes, measured, which is what this is for: one queue
    per camera of the changes still to write, and the outcome of the last
    flush, kept until the sheet has read it once. It lives in memory and does
    not survive the proxy restarting.
    """

    def __init__(self) -> None:
        self._queued: dict[str, dict[str, Any]] = {}
        self._results: dict[str, dict[str, Any]] = {}

    def queue(self, slug: str, changes: dict[str, Any]) -> None:
        """Hold validated changes; the latest value of a control wins."""
        entry = self._queued.setdefault(slug, {})
        for key, value in changes.items():
            if key == "light_settings" and isinstance(value, dict):
                entry.setdefault("light_settings", {}).update(value)
            else:
                entry[key] = value

    def queued(self, slug: str) -> dict[str, Any]:
        """A copy of what is waiting for the camera."""
        entry = self._queued.get(slug, {})
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in entry.items()}

    def names(self, slug: str) -> list[str]:
        return control_names(self._queued.get(slug, {}))

    def take(self, slug: str) -> dict[str, Any]:
        """Remove and return the camera's queue, for the flush."""
        return self._queued.pop(slug, {})

    def record(self, slug: str, result: dict[str, Any]) -> None:
        self._results[slug] = result

    def take_result(self, slug: str) -> dict[str, Any] | None:
        """Hand over the last flush's outcome once, to whoever reads next."""
        return self._results.pop(slug, None)


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


def write_plan(
    row: dict[str, Any], clean: dict[str, Any]
) -> list[tuple[str, Any, list[str]]]:
    """Turn validated changes into the Blink calls that carry them.

    Returns ``(kind, payload, controls)`` triples: ``lights`` flips the
    floodlight lamp through its own on/off route, ``owl_config`` posts a JSON
    document to the owl config route, ``camera_update`` posts one to the classic
    camera update route, and ``camera_config_v2`` posts one to the v2 camera
    config route the app uses for its Speaker Volume slider.

    ``controls`` names the player's controls riding on that one call, so a
    refusal can be reported against the thing the viewer actually touched rather
    than the Blink field it happens to be stored in.
    """
    floodlight = _product_type(row) in FLOODLIGHT_TYPES
    plan: list[tuple[str, Any, list[str]]] = []
    owl: dict[str, Any] = {}
    owl_controls: list[str] = []
    classic: dict[str, Any] = {}
    classic_controls: list[str] = []
    if "flood_light" in clean:
        plan.append(("lights", clean["flood_light"], ["flood_light"]))
    if "night_vision" in clean:
        word = clean["night_vision"]
        if floodlight:
            owl["illuminator_enable"] = word
            owl_controls.append("night_vision")
        else:
            classic["illuminator_enable"] = {"off": 0, "on": 1, "auto": 2}[word]
            classic_controls.append("night_vision")
    if "brightness" in clean:
        owl.setdefault("superior", {})["illuminator_intensity"] = clean["brightness"]
        owl["light_brightness"] = clean["brightness"]
        owl_controls.append("brightness")
    if "volume" in clean:
        if floodlight:
            owl["volume_control"] = clean["volume"]
            owl_controls.append("volume")
        else:
            plan.append(
                ("camera_config_v2", {"lfr_sync_interval": clean["volume"]}, ["volume"])
            )
    for key, value in clean.get("light_settings", {}).items():
        owl.setdefault("superior", {})[LIGHT_SETTING_FIELDS[key]] = value
        owl_controls.append(key)
    if owl:
        plan.append(("owl_config", owl, owl_controls))
    if classic:
        plan.append(("camera_update", classic, classic_controls))
    return plan


async def fetch_state(
    blink: Any,
    row: dict[str, Any],
    liveview: Any | None = None,
    deferred: DeferredControls | None = None,
) -> dict[str, Any]:
    """Read the camera's current settings from Blink.

    ``deferred`` is the registry of changes held back for after the live view,
    if the caller keeps one. What is waiting there is shown over the camera's
    own values and named in ``deferred``, so the sheet keeps showing what was
    asked for rather than snapping back to what the camera still reports.

    ``liveview`` is the proxy's open live view on this camera, if it has one:
    anything with a ``flood_light`` attribute holding what the camera has
    reported over that session, or None. That report wins over the config
    document's ``light_status`` while the session is open. The document is
    served from Blink's cloud and does not record a lamp worked this way: it
    still read off 23 s after this proxy had lit the lamp over a session and
    the camera had confirmed it. The lamp itself lasts for that live view. It
    was off again, by the camera's own report and by the picture, when the
    next session opened 8 s after the first ended.
    """
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
    state = read_state(row, config, owl_config)
    reported = getattr(liveview, "flood_light", None)
    if reported is not None and state["capabilities"]["flood_light"]:
        state["flood_light"] = reported
    if deferred is not None:
        held = deferred.queued(str(row.get("slug")))
        _overlay(state, held)
        state["deferred"] = control_names(held)
    return state


def _unreflected(clean: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """Name the requested controls the camera is not yet reporting back."""
    missing: list[str] = []
    for key, value in clean.items():
        if key == "light_settings":
            settings = state.get("light_settings")
            for name, wanted in value.items():
                if not isinstance(settings, dict) or settings.get(name) != wanted:
                    missing.append(name)
        elif state.get(key) != value:
            missing.append(key)
    return missing


async def _command_finished(
    blink: Any, network: Any, answer: Any, deadline: float
) -> bool:
    """Wait, briefly, for Blink to actually carry out a queued change.

    Blink answers a write immediately and queues the work as a command; the
    camera's config keeps the old value until it is done. Read back before then
    and the control snaps to where it was, which is what the Blink app avoids by
    polling the command first.

    blinkpy's own wait gives up after MAX_RETRY seconds, far too long to hold a
    request open. ``deadline`` is one budget for the whole call rather than per
    write, because the route above allows thirty seconds for everything and a
    body naming several controls plans several writes. A change still in flight
    when the budget runs out is reported as pending instead of waited on.
    """
    command_id = answer.get("id") if isinstance(answer, dict) else None
    if not command_id:
        return True
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        return False
    try:
        return await asyncio.wait_for(
            blink_api.wait_for_command(
                blink, {"network_id": network, "id": command_id}
            ),
            timeout=remaining,
        )
    except (TimeoutError, asyncio.TimeoutError):
        return False
    except Exception:  # noqa: BLE001
        LOGGER.debug("Could not follow command %s", command_id, exc_info=True)
        return False


def _answer_busy(answer: Any) -> bool:
    """Blink returns 307 when the camera is mid-command and cannot take another.

    Worth separating from a flat refusal: this one is worth trying again.
    """
    return isinstance(answer, dict) and answer.get("code") == 307


def _answer_accepted(answer: Any) -> bool:
    """Blink answers 200 to everything; only the body says whether it took."""
    if not isinstance(answer, dict):
        return False
    if _answer_busy(answer):
        return False
    return answer.get("command") is not None or answer.get("state") == "new"


async def _lamp_in_band(liveview: Any, on: bool, row: dict[str, Any]) -> bool:
    """Work the lamp over the open live view; say whether that was possible.

    Blink refuses the lights route with 307 for as long as any live view is
    open on the camera, and the Controls sheet only exists inside one, so from
    the player that route can never succeed. The Blink app never uses it while
    watching: it sends the lamp as an inline command over the live-view session
    itself, and so does this when the proxy holds that session. A session that
    cannot carry it, RTSP or one already closed, is reported here and the
    caller falls back to the route.
    """
    try:
        await liveview.set_flood_light(on)
    except (NotImplementedError, RuntimeError) as err:
        LOGGER.info(
            "Could not send the flood light over the live view for %s (%s); "
            "trying Blink's lights route",
            row.get("slug"),
            err,
        )
        return False
    return True


async def apply_changes(
    blink: Any,
    row: dict[str, Any],
    changes: Any,
    liveview: Any | None = None,
    deferred: DeferredControls | None = None,
) -> dict[str, Any]:
    """Send the requested changes to Blink and return what it holds now.

    ``liveview`` is the proxy's open live view on this camera, or None. With
    one, the lamp goes over it (see ``_lamp_in_band``) and the read back takes
    the lamp state the camera reports over it.

    ``deferred`` is where a change Blink calls busy during that live view is
    held for writing once the live view ends. Given both, such a change is
    reported in ``deferred`` rather than ``busy``, and the answer shows the
    value that was asked for.
    """
    clean = validate_changes(row, changes)
    network = row["network_id"]
    camera_id = row["id"]
    rejected: list[str] = []
    busy: list[str] = []
    pending: list[str] = []
    deadline = asyncio.get_running_loop().time() + COMMAND_WAIT_SECONDS
    for kind, payload, controls in write_plan(row, clean):
        if kind == "lights":
            if liveview is not None and await _lamp_in_band(liveview, payload, row):
                continue
            answer = await blink_api.request_floodlight(
                blink, network, camera_id, payload
            )
            if _answer_busy(answer):
                busy.extend(controls)
                rejected.extend(controls)
            elif not await _command_finished(blink, network, answer, deadline):
                pending.extend(controls)
            continue
        # blinkpy documents the body as a JSON string. A dict is sent
        # form-encoded and Blink answers 200 while ignoring it.
        body = json.dumps(payload)
        if kind == "camera_config_v2":
            # The only route blinkpy does not carry; see the module docstring.
            url = (
                f"{blink.urls.base_url}/api/v2/accounts/{blink.account_id}"
                f"/networks/{network}/cameras/{camera_id}/config"
            )
            response = await blink_api.http_post(blink, url, json=False, data=body)
        else:
            # blinkpy picks the route from product_type. It whitelists "owl" and
            # "catalina" only, but the route "catalina" selects is the classic
            # camera update every non-owl family answers, xt and xt2 and white
            # included, so they go through under that word.
            response = await blink_api.request_update_config(
                blink,
                network,
                camera_id,
                product_type="owl" if kind == "owl_config" else "catalina",
                data=body,
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
            rejected.extend(controls)
            if _answer_busy(answer):
                busy.extend(controls)
        elif not await _command_finished(blink, network, answer, deadline):
            pending.extend(controls)
    held_names: list[str] = []
    if busy and liveview is not None and deferred is not None:
        # The live view holds the camera, and the sheet only exists inside one,
        # so a retry from there can never succeed. What Blink called busy is
        # held and written once the last session on the camera has closed.
        held = _subset(clean, busy)
        deferred.queue(str(row.get("slug")), held)
        held_names = control_names(held)
        LOGGER.info(
            "Holding %s for %s until its live view ends", held_names, row.get("slug")
        )
        rejected = [c for c in rejected if c not in held_names]
        busy = [c for c in busy if c not in held_names]
    # Read back until the camera actually reports what was asked for. One read
    # straight after the write is not enough: the owl config route keeps serving
    # the old value for a few seconds after accepting a change, and that stale
    # number then overwrites the control the viewer just moved.
    settled = rejected + held_names
    state = await fetch_state(blink, row, liveview, deferred)
    waiting = [c for c in _unreflected(clean, state) if c not in settled]
    while waiting and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(READ_BACK_INTERVAL)
        state = await fetch_state(blink, row, liveview, deferred)
        waiting = [c for c in _unreflected(clean, state) if c not in settled]
    pending = waiting
    state["rejected"] = rejected
    state["busy"] = busy
    state["pending"] = pending
    # Whatever never showed up keeps the value that was asked for, so a control
    # does not snap back to the old one while Blink is still catching up.
    for name in pending:
        if name in clean:
            state[name] = clean[name]
        elif name in LIGHT_SETTING_FIELDS and isinstance(
            state.get("light_settings"), dict
        ):
            state["light_settings"][name] = clean["light_settings"][name]
    return state


async def flush_deferred(
    blink: Any,
    row: dict[str, Any],
    changes: dict[str, Any],
    *,
    retry_interval: float = DEFERRED_RETRY_SECONDS,
    give_up_after: float = DEFERRED_GIVE_UP_SECONDS,
) -> dict[str, Any]:
    """Write held-back changes now that the live view has closed.

    Values the camera already reports are dropped first: Blink answers a
    same-value write with ``state: done`` and no command, which reads as a
    refusal. Whatever Blink still calls busy, because another client is
    streaming from the camera, is retried until ``give_up_after`` and then
    reported as such. The answer names what was set and what was not.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    state = await fetch_state(blink, row)
    wanted = _subset(changes, _unreflected(changes, state))
    wanted_names = control_names(wanted)
    result: dict[str, Any] = {
        "applied": [c for c in control_names(changes) if c not in wanted_names],
        "failed": [],
        "busy": [],
        "at": now_stamp(),
    }
    if not wanted:
        return result
    while True:
        state = await apply_changes(blink, row, wanted)
        busy = list(state.get("busy") or [])
        if not busy or loop.time() - started >= give_up_after:
            break
        await asyncio.sleep(retry_interval)
    rejected = list(state.get("rejected") or [])
    result["failed"] = rejected
    result["busy"] = busy
    result["applied"].extend(c for c in wanted_names if c not in rejected)
    return result
