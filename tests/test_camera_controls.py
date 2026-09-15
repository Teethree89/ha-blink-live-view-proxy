"""The camera controls fold two Blink config shapes into one flat state.

The Wired Floodlight answers the owl config route with its lamp settings under
a ``superior`` block; every other camera answers the classic camera config
route with integer codes. These tests pin the translation both ways, the
field whitelist on writes, and the accepted/rejected reading of Blink's
answers, without touching Blink. Run from the repo root:

    python tests/test_camera_controls.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "proxy"))

from blink_proxy import camera_controls as cc  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0

FLOOD_ROW = {
    "slug": "flood_light",
    "name": "Flood light",
    "id": "318367",
    "network_id": "1",
    "product_type": "superior",
}
INDOOR_ROW = {
    "slug": "back_door",
    "name": "Back Door",
    "id": "2",
    "network_id": "1",
    "product_type": "white",
}
OWL_CONFIG = {
    "illuminator_enable": "auto",
    "illuminator_intensity": 4,
    "light_brightness": 3,
    "light_status": "off",
    "volume_control": 8,
    "superior": {
        "motion_alert": True,
        "illuminator_intensity": 3,
        "illuminator_duration": 30,
        "manual_illuminator_duration": 180,
        "illuminator_duration_options": [30, 60, 180],
        "manual_illuminator_duration_options": [65535, 30, 60, 180],
        "auto_on_off_enabled": False,
    },
}
CLASSIC_CONFIG = {
    "camera": {"illuminator_enable": 2, "illuminator_intensity": 7, "temperature": 71, "lfr_sync_interval": 8},
    "signals": {"temp": 71},
}
GRILL_ROW = {
    "slug": "grill_camera",
    "name": "Grill Camera",
    "id": "3",
    "network_id": "1",
    "product_type": "catalina",
}


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def test_read_state() -> None:
    print("read_state")
    flood = cc.read_state(FLOOD_ROW, None, OWL_CONFIG)
    check(flood["flood_light"] is False, "lamp off reads as false")
    check(flood["night_vision"] == "auto", "owl night vision word passes through")
    check(flood["brightness"] == 3, "lamp brightness comes from the superior block, not IR")
    check(flood["volume"] == 8, "volume_control is the volume")
    check(flood["temperature_f"] is None, "floodlight has no temperature")
    check(flood["light_settings"]["dusk_to_dawn"] is False, "dusk to dawn maps auto_on_off_enabled")
    check(flood["light_settings"]["manual_duration"] == 180, "manual timeout comes from superior")
    check(flood["capabilities"]["volume"] and not flood["capabilities"]["temperature"], "floodlight capabilities")

    indoor = cc.read_state(INDOOR_ROW, CLASSIC_CONFIG, None)
    check(indoor["night_vision"] == "auto", "classic code 2 is auto")
    check(indoor["temperature_f"] == 71, "temperature comes from signals")
    check(indoor["brightness"] is None and indoor["volume"] is None, "no lamp or volume on an indoor camera")
    check(not indoor["capabilities"]["flood_light"], "indoor camera offers no flood light")

    grill = cc.read_state(GRILL_ROW, CLASSIC_CONFIG, None)
    check(grill["volume"] == 8 and grill["capabilities"]["volume"], "a speaker camera reads lfr_sync_interval as its volume")
    listed = cc.read_state(INDOOR_ROW, {"camera": [{"illuminator_enable": 0}]}, None)
    check(listed["night_vision"] == "off", "a list-shaped camera block still reads")
    check(cc.read_state(FLOOD_ROW, None, None)["night_vision"] is None, "no config means unknown, not a crash")


def test_validate() -> None:
    print("validate_changes")
    clean = cc.validate_changes(FLOOD_ROW, {"flood_light": True, "brightness": 7, "volume": 5, "night_vision": "off"})
    check(clean == {"flood_light": True, "brightness": 7, "volume": 5, "night_vision": "off"}, "floodlight accepts all four")
    for body, label in [
        ({}, "an empty body"),
        ({"volume": 0}, "volume 0"),
        ({"volume": 9}, "volume 9"),
        ({"brightness": 11}, "brightness 11"),
        ({"brightness": "5"}, "a string brightness"),
        ({"night_vision": "dim"}, "an unknown night vision word"),
        ({"flood_light": 1}, "a non-boolean flood light"),
        ({"siren": True}, "an unknown control"),
        ({"light_settings": {"dusk_to_dawn": "yes"}}, "a non-boolean light setting"),
        ({"light_settings": {"manual_duration": -1}}, "a negative timeout"),
    ]:
        try:
            cc.validate_changes(FLOOD_ROW, body)
            check(False, f"{label} is refused")
        except cc.ControlError:
            check(True, f"{label} is refused")
    for body, label in [
        ({"flood_light": True}, "flood light on an indoor camera"),
        ({"volume": 4}, "volume on an indoor camera"),
        ({"brightness": 4}, "brightness on an indoor camera"),
    ]:
        try:
            cc.validate_changes(INDOOR_ROW, body)
            check(False, f"{label} is refused")
        except cc.ControlError:
            check(True, f"{label} is refused")
    check(cc.validate_changes(INDOOR_ROW, {"night_vision": 1}) == {"night_vision": "on"}, "indoor accepts a night vision code")


def test_write_plan() -> None:
    print("write_plan")
    plan = cc.write_plan(FLOOD_ROW, {"flood_light": True, "night_vision": "off", "brightness": 6, "volume": 2,
                                     "light_settings": {"dusk_to_dawn": True, "manual_duration": 65535}})
    kinds = [kind for kind, _, _ in plan]
    payloads = {kind: payload for kind, payload, _ in plan}
    controls = {kind: names for kind, _, names in plan}
    check(kinds == ["lights", "owl_config"], "lamp goes through its own route, the rest in one owl post")
    check(controls["lights"] == ["flood_light"], "the lamp call carries its own control name")
    check(sorted(controls["owl_config"]) == ["brightness", "dusk_to_dawn", "manual_duration", "night_vision", "volume"],
          "the owl call names every control riding on it")
    owl = payloads["owl_config"]
    check(owl["illuminator_enable"] == "off", "owl night vision is a word")
    check(owl["superior"]["illuminator_intensity"] == 6 and owl["light_brightness"] == 6, "brightness lands in both places")
    check(owl["volume_control"] == 2, "volume_control carries the volume")
    check(owl["superior"]["auto_on_off_enabled"] is True and owl["superior"]["manual_illuminator_duration"] == 65535, "light settings nest under superior")
    check(payloads["lights"] is True, "lamp payload is the boolean")

    plan = cc.write_plan(INDOOR_ROW, {"night_vision": "auto"})
    check(plan == [("camera_update", {"illuminator_enable": 2}, ["night_vision"])], "indoor night vision is a code on the update route")
    plan = cc.write_plan(GRILL_ROW, {"volume": 3})
    check(plan == [("camera_config_v2", {"lfr_sync_interval": 3}, ["volume"])], "speaker volume goes to the v2 config route as lfr_sync_interval")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def json(self, content_type=None):
        return self.payload


class FakeUrls:
    base_url = "https://blink.example"


class FakeBlink:
    urls = FakeUrls()
    account_id = "77"


def test_apply(monkey_calls: list) -> None:
    print("apply_changes")
    # Blink is faked here and never settles, so every write would otherwise
    # read back until the real deadline. Keep the loop, shrink the clock.
    cc.COMMAND_WAIT_SECONDS = 0.2
    cc.READ_BACK_INTERVAL = 0.01
    answers = {"owl_config": {"command": "config_set", "state": "new"}, "camera_update": {"state": "done"},
               "camera_config_v2": {"id": 1, "command": "config_set", "state": "new"}}

    async def fake_post(blink, url, is_retry=False, data=None, json=True, timeout=10):
        kind = "owl_config" if "/owls/" in url else "camera_config_v2" if "/api/v2/" in url else "camera_update"
        monkey_calls.append((kind, url, data))
        return FakeResponse(answers[kind])

    async def fake_lights(blink, network, camera_id, enable):
        monkey_calls.append(("lights", enable))
        return {"code": 307, "message": "System is busy, please wait"}

    async def fake_get_config(blink, network, camera_id, product_type="owl"):
        return OWL_CONFIG

    async def fake_camera_info(blink, network, camera_id):
        return CLASSIC_CONFIG

    async def fake_wait(blink, json_data):
        monkey_calls.append(("wait", json_data))
        return True

    cc.blink_api.http_post = fake_post
    cc.blink_api.request_floodlight = fake_lights
    cc.blink_api.request_get_config = fake_get_config
    cc.blink_api.request_camera_info = fake_camera_info
    cc.blink_api.wait_for_command = fake_wait

    state = asyncio.run(cc.apply_changes(FakeBlink(), FLOOD_ROW, {"flood_light": True, "volume": 3}))
    kinds = [call[0] for call in monkey_calls]
    check(kinds == ["lights", "owl_config"], "both calls were made in order")
    body = monkey_calls[1][2]
    check(isinstance(body, str) and json.loads(body) == {"volume_control": 3}, "the owl body is a JSON string, not a dict")
    check("/accounts/77/networks/1/owls/318367/config" in monkey_calls[1][1], "owl config URL carries account, network, camera")
    check(state["rejected"] == ["flood_light"], "a busy lamp is reported as rejected")
    check(state["busy"] == ["flood_light"], "and is separately marked worth retrying")
    # The fixture config never changes, so the write is never reflected: the
    # answer must keep what was asked for rather than the config's stale 8.
    check(state["volume"] == 3 and state["pending"] == ["volume"],
          "a value the config has not caught up with is kept, and flagged pending")

    monkey_calls.clear()
    state = asyncio.run(cc.apply_changes(FakeBlink(), INDOOR_ROW, {"night_vision": "off"}))
    check(monkey_calls[0][1].endswith("/network/1/camera/2/update"), "indoor writes go to the classic update route")
    check(state["rejected"] == ["night_vision"], "a refusal names the control, not the Blink field")
    check(state["busy"] == [], "a done-without-command answer is a refusal, not a busy camera")

    monkey_calls.clear()
    state = asyncio.run(cc.apply_changes(FakeBlink(), GRILL_ROW, {"volume": 5}))
    check("/api/v2/accounts/77/networks/1/cameras/3/config" in monkey_calls[0][1], "speaker volume posts to the v2 camera config route")
    check(json.loads(monkey_calls[0][2]) == {"lfr_sync_interval": 5} and state["rejected"] == [], "the v2 body is the one field and a command answer is accepted")
    waits = [call for call in monkey_calls if call[0] == "wait"]
    check(waits and waits[0][1] == {"network_id": GRILL_ROW["network_id"], "id": 1},
          "an accepted write is followed through its command before the re-read")
    check(state["volume"] == 5 and state["pending"] == ["volume"],
          "the v2 write is kept too while the config still reports the old level")

    # The floodlight case: Blink accepts the write and keeps serving the old
    # value for a while. The answer must not carry that stale number back.
    monkey_calls.clear()
    state = asyncio.run(cc.apply_changes(FakeBlink(), FLOOD_ROW, {"volume": 2}))
    check(state["volume"] == 2,
          "a config still reporting the old volume does not snap the control back")
    check(state["pending"] == ["volume"],
          "and the control is reported pending rather than silently wrong")

    # Once the camera does report it, nothing is left pending.
    settled = dict(OWL_CONFIG, volume_control=2)
    original_get = cc.blink_api.request_get_config

    async def settled_get(blink, network, camera_id, product_type="owl"):
        return settled

    cc.blink_api.request_get_config = settled_get
    state = asyncio.run(cc.apply_changes(FakeBlink(), FLOOD_ROW, {"volume": 2}))
    check(state["volume"] == 2 and state["pending"] == [],
          "a value the camera confirms is not reported pending")
    cc.blink_api.request_get_config = original_get

    try:
        asyncio.run(cc.apply_changes(FakeBlink(), INDOOR_ROW, {"volume": 3}))
        check(False, "apply refuses an unsupported control before calling Blink")
    except cc.ControlError:
        check(True, "apply refuses an unsupported control before calling Blink")


def test_capability_map() -> None:
    """Pin who gets what, because the README describes this table."""
    print("capabilities")
    flood = cc.capabilities("superior")
    xt2 = cc.capabilities("xt2")
    white = cc.capabilities("white")
    check(flood["night_vision"] and xt2["night_vision"] and white["night_vision"],
          "night vision is on every camera, the floodlight included")
    check(not flood["temperature"] and xt2["temperature"] and white["temperature"],
          "the floodlight is the one camera with no thermometer")
    check(flood["volume"] and xt2["volume"] and not white["volume"],
          "volume is the floodlight and the speaker models, not white")
    check(flood["brightness"] and flood["light_settings"]
          and not xt2["brightness"] and not xt2["light_settings"],
          "the lamp and its settings are the floodlight's alone")


def test_blinkpy_routes() -> None:
    """Config writes go through blinkpy's own function, not a hand-built URL."""
    print("blinkpy routes")
    calls: list = []
    posts: list = []

    async def fake_update_config(blink, network, camera_id, product_type="owl", data=None):
        calls.append((product_type, camera_id, data))
        return FakeResponse({"command": "config_set", "state": "new"})

    async def fake_post(blink, url, is_retry=False, data=None, json=True, timeout=10):
        posts.append(url)
        return FakeResponse({"command": "config_set", "state": "new"})

    async def fake_get_config(blink, network, camera_id, product_type="owl"):
        return OWL_CONFIG

    async def fake_camera_info(blink, network, camera_id):
        return CLASSIC_CONFIG

    cc.blink_api.request_update_config = fake_update_config
    cc.blink_api.http_post = fake_post
    cc.blink_api.request_get_config = fake_get_config
    cc.blink_api.request_camera_info = fake_camera_info

    asyncio.run(cc.apply_changes(FakeBlink(), FLOOD_ROW, {"brightness": 5}))
    check(calls and calls[0][0] == "owl", "the floodlight's config goes through blinkpy as an owl")
    check(not posts, "no hand-built URL is used for an owl config write")

    calls.clear()
    asyncio.run(cc.apply_changes(FakeBlink(), INDOOR_ROW, {"night_vision": "off"}))
    check(calls and calls[0][0] == "catalina",
          "a white camera reaches the classic route through blinkpy, under catalina")
    check(not posts, "no hand-built URL is used for a classic config write")

    calls.clear()
    asyncio.run(cc.apply_changes(FakeBlink(), GRILL_ROW, {"volume": 5}))
    check(not calls, "speaker volume does not pretend to be a blinkpy config write")
    check(len(posts) == 1 and "/api/v2/" in posts[0],
          "the v2 speaker volume route is the only write blinkpy has no function for")


def main() -> int:
    test_read_state()
    test_validate()
    test_write_plan()
    test_apply([])
    test_capability_map()
    test_blinkpy_routes()
    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\nfailed:")
        for failure in FAILURES:
            print(f"  {failure}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
