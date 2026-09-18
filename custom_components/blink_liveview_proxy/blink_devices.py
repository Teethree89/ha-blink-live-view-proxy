"""Names, ids and values for the Blink entities built from the proxy's session.

Everything here is a decision rather than Home Assistant plumbing, so the
tests can load this file on its own: which entity ids a household gets, what
the unique ids are, and how a /devices row turns into a state.

Entity ids are set by the entities themselves rather than left to the
friendly name, and every one starts "blink_proxy_". The official integration already
owns camera.<name>, sensor.blink_<name>_temperature and
alarm_control_panel.blink_<sync>, and most people who turn this on still have
it installed, even if disabled. Borrowing its names would hand them a
"_2" suffix that moves depending on which integration loaded first.
"""

from __future__ import annotations

import re
from typing import Any

OBJECT_ID_PREFIX = "blink_proxy"


def slugify(value: Any) -> str:
    """Lower-case, with every run of anything else collapsed to one underscore."""
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def camera_key(camera: dict[str, Any]) -> str:
    """The identifier a camera's device and unique ids are built from.

    The same choice the live camera makes, so the new entities land on the
    device that already exists for that camera.
    """
    return str(camera.get("serial") or camera.get("id") or camera.get("slug") or "camera")


def camera_object_id(slug: str, suffix: str = "") -> str:
    """blink_proxy_<slug>[_<suffix>], the object id of a camera's entity."""
    base = f"{OBJECT_ID_PREFIX}_{slugify(slug)}"
    return f"{base}_{suffix}" if suffix else base


def camera_unique_id(entry_id: str, camera: dict[str, Any], suffix: str) -> str:
    return f"{entry_id}_{camera_key(camera)}_{suffix}"


def sync_object_id(sync: dict[str, Any]) -> str:
    name = slugify(sync.get("name")) or slugify(sync.get("network_id")) or "sync"
    return f"{OBJECT_ID_PREFIX}_{name}"


def sync_unique_id(entry_id: str, sync: dict[str, Any]) -> str:
    return f"{entry_id}_sync_{sync.get('network_id')}_arm"


def sync_device_key(sync: dict[str, Any]) -> str:
    return f"sync_{sync.get('network_id')}"


def devices_cameras(devices: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(devices, dict):
        return []
    return [row for row in devices.get("cameras") or [] if isinstance(row, dict)]


def devices_syncs(devices: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(devices, dict):
        return []
    return [row for row in devices.get("sync_modules") or [] if isinstance(row, dict)]


def find_camera(devices: dict[str, Any] | None, slug: str) -> dict[str, Any] | None:
    for row in devices_cameras(devices):
        if row.get("slug") == slug:
            return row
    return None


def find_sync(devices: dict[str, Any] | None, network_id: str) -> dict[str, Any] | None:
    for row in devices_syncs(devices):
        if str(row.get("network_id")) == str(network_id):
            return row
    return None


def replace_camera(devices: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """A copy of `devices` with one camera's row swapped for a newer one.

    An action answers with the camera's state after Blink applied it, which is
    newer than anything the last poll saw.
    """
    cameras = [
        row if existing.get("slug") == row.get("slug") else existing
        for existing in devices_cameras(devices)
    ]
    return {**devices, "cameras": cameras}


def replace_sync(devices: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    syncs = [
        row if str(existing.get("network_id")) == str(row.get("network_id")) else existing
        for existing in devices_syncs(devices)
    ]
    return {**devices, "sync_modules": syncs}


def poll_failed(devices: dict[str, Any] | None) -> bool:
    """Whether the proxy has given up refreshing until its session is replaced.

    Last-known values stay on screen through an ordinary failed poll, which
    backs off and tries again. After a failed token refresh they would only
    grow older, so the entities go unavailable instead of looking current.
    """
    if not isinstance(devices, dict):
        return False
    return (devices.get("poll") or {}).get("state") == "auth_failed"


def alarm_state(sync: dict[str, Any] | None) -> str | None:
    """"armed_away", "disarmed", or None when Blink has not said."""
    if not sync:
        return None
    armed = sync.get("armed")
    if armed is True:
        return "armed_away"
    if armed is False:
        return "disarmed"
    return None


def battery_voltage(row: dict[str, Any] | None) -> float | None:
    """Volts. blinkpy reports hundredths, so 165 is 1.65 V."""
    value = (row or {}).get("battery_voltage")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(value / 100, 2)


# One row per sensor: suffix (object and unique id), friendly-name suffix,
# the /devices field it reads, and the kind of value it is. The platform
# turns the kind into Home Assistant's device class and unit.
CAMERA_SENSORS: tuple[tuple[str, str, str, str], ...] = (
    ("temperature", "Temperature", "temperature", "temperature_f"),
    ("wifi_signal", "Wi-Fi signal", "wifi_strength", "signal_dbm"),
    ("battery_voltage", "Battery voltage", "battery_voltage", "voltage"),
)


def sensor_value(row: dict[str, Any] | None, field: str) -> Any:
    if row is None:
        return None
    if field == "battery_voltage":
        return battery_voltage(row)
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value
