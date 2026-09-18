"""Entity ids and values for the Blink entities, without Home Assistant.

blink_devices.py holds every decision the platforms make: which entity ids a
household gets, which unique ids they are registered under, and how a row from
the proxy's /devices turns into a state. It imports nothing from Home
Assistant, so it is loaded here on its own. The platforms themselves are
exercised inside real Home Assistant by tests/ha/. Run from the repo root:

    python tests/test_blink_devices.py
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "blink_liveview_proxy"

spec = importlib.util.spec_from_file_location("blink_devices", COMPONENT / "blink_devices.py")
bd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bd)

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


DEVICES = {
    "cameras": [
        {"slug": "driveway", "name": "Driveway", "id": "909058", "serial": "G8V1GH0225031KA4",
         "motion_enabled": True, "battery_voltage": 165, "temperature": 68,
         "wifi_strength": -58},
        {"slug": "front_droor", "name": "Front droor", "id": "143892", "serial": None},
    ],
    "sync_modules": [{"name": "114 Cooper", "network_id": "2001", "armed": False}],
    "poll": {"state": "ok"},
}


def test_ids() -> None:
    print("\nentity ids")
    check(bd.camera_object_id("driveway") == "blink_proxy_driveway",
          "a camera's snapshot is blink_proxy_<slug>")
    check(bd.camera_object_id("riccis_window", "motion_detection")
          == "blink_proxy_riccis_window_motion_detection",
          "its other entities add a suffix")
    check(bd.sync_object_id({"name": "114 Cooper", "network_id": "2001"})
          == "blink_proxy_114_cooper",
          "a sync module is named from its Blink name")
    check(bd.sync_object_id({"name": "", "network_id": "2001"}) == "blink_proxy_2001",
          "and from its network id when it has no name")
    official = {"driveway", "blink_driveway_temperature", "blink_114_cooper",
                "driveway_camera_motion_detection", "driveway_motion", "driveway_battery"}
    ours = {bd.camera_object_id("driveway", s) for s in
            ("", "temperature", "motion_detection", "motion", "battery")}
    ours.add(bd.sync_object_id({"name": "114 Cooper"}))
    check(not ours & official,
          "no object id collides with the official integration's")

    print("\nunique ids and devices")
    camera = DEVICES["cameras"][0]
    check(bd.camera_key(camera) == "G8V1GH0225031KA4",
          "a camera's key is its serial, the same as the live camera's device")
    check(bd.camera_key(DEVICES["cameras"][1]) == "143892",
          "then its id when Blink sent no serial")
    check(bd.camera_unique_id("E", camera, "motion") == "E_G8V1GH0225031KA4_motion",
          "unique ids are entry, key, suffix")
    check(bd.sync_unique_id("E", DEVICES["sync_modules"][0]) == "E_sync_2001_arm",
          "a sync module's unique id carries its network id")


def test_values() -> None:
    print("\nvalues")
    check(bd.find_camera(DEVICES, "driveway")["id"] == "909058", "cameras are found by slug")
    check(bd.find_camera(DEVICES, "nowhere") is None, "an unknown slug is None")
    check(bd.find_camera(None, "driveway") is None, "no device state at all is None")
    check(bd.find_sync(DEVICES, 2001)["name"] == "114 Cooper",
          "sync modules are found by network id, whatever its type")
    check(bd.alarm_state({"armed": True}) == "armed_away", "armed is armed_away")
    check(bd.alarm_state({"armed": False}) == "disarmed", "not armed is disarmed")
    check(bd.alarm_state({"armed": None}) is None and bd.alarm_state(None) is None,
          "unknown stays unknown rather than guessing disarmed")
    check(bd.sensor_value(DEVICES["cameras"][0], "battery_voltage") == 1.65,
          "battery voltage is converted from hundredths")
    check(bd.sensor_value(DEVICES["cameras"][0], "temperature") == 68, "temperature passes through")
    check(bd.sensor_value(DEVICES["cameras"][1], "temperature") is None,
          "a missing reading is None, not zero")
    check(bd.sensor_value({"temperature": True}, "temperature") is None,
          "a boolean is not a reading")

    print("\nactions and poll state")
    updated = bd.replace_camera(DEVICES, {"slug": "driveway", "motion_enabled": False})
    check(bd.find_camera(updated, "driveway")["motion_enabled"] is False,
          "an action's answer replaces that camera's row")
    check(bd.find_camera(DEVICES, "driveway")["motion_enabled"] is True,
          "without touching the previous state")
    check(len(updated["cameras"]) == 2 and updated["poll"] == DEVICES["poll"],
          "and nothing else")
    synced = bd.replace_sync(DEVICES, {"network_id": "2001", "armed": True})
    check(bd.find_sync(synced, "2001")["armed"] is True, "the same for a sync module")
    check(not bd.poll_failed(DEVICES), "an ok poll has not failed")
    check(bd.poll_failed({**DEVICES, "poll": {"state": "auth_failed"}}),
          "auth_failed is a failed session")
    check(not bd.poll_failed({**DEVICES, "poll": {"state": "backoff"}}),
          "an ordinary backoff keeps the last values on screen")


def test_platforms() -> None:
    print("\nevery platform is set up, with no switch to turn them off")
    tree = ast.parse((COMPONENT / "const.py").read_text(encoding="utf-8"))
    names = {
        node.targets[0].id: node
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    platforms = [elt.attr for elt in names["PLATFORMS"].value.elts]
    check(platforms[0] == "CAMERA", "CAMERA first, so the proxy device exists for the rest")
    check(set(platforms) == {"CAMERA", "BINARY_SENSOR", "ALARM_CONTROL_PANEL",
                             "BUTTON", "SENSOR", "SWITCH"},
          "all six platforms")
    check("DEFAULT_BLINK_ENTITIES" not in names and "CONF_BLINK_ENTITIES" not in names,
          "there is no option to turn the Blink entities off")
    for name in ("camera.py", "binary_sensor.py", "__init__.py"):
        text = (COMPONENT / name).read_text(encoding="utf-8")
        check("blink_entities" not in text, f"{name} does not gate on an option")


def main() -> int:
    test_ids()
    test_values()
    test_platforms()
    if FAILURES:
        print(f"\n{len(FAILURES)} of {CHECKS} failed")
        return 1
    print(f"\nall passed ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
