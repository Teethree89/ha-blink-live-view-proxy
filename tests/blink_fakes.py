"""A blinkpy session with no network behind it, for the device tests.

Shared by tests/test_device_poll.py and the Home Assistant tests under
tests/ha/, which run the real proxy routes against it. Only the attributes and
coroutines the proxy's devices module touches are here.
"""

from __future__ import annotations

from typing import Any


class FakeSync:
    def __init__(self, name: str, network_id: str, armed: bool) -> None:
        self.name = name
        self.network_id = network_id
        self.sync_id = "77"
        self.serial = "SYNC-SERIAL"
        self.status = "online"
        self.available = True
        self.network_info = {"network": {"armed": armed}}
        self.arm_calls: list[bool] = []
        self.blink: Any = None

    async def async_arm(self, value: bool) -> dict[str, Any]:
        self.arm_calls.append(value)
        self._pending = value
        return {"id": 1, "network_id": self.network_id}

    async def get_network_info(self) -> bool:
        self.network_info = {"network": {"armed": getattr(self, "_pending", False)}}
        return True

    def get_unique_info(self, _name: str) -> None:
        return None

    async def get_camera_info(self, camera_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {"id": camera_id}


class FakeCamera:
    def __init__(self, name: str, camera_id: str, serial: str, sync: FakeSync) -> None:
        self.name = name
        self.camera_id = camera_id
        self.serial = serial
        self.network_id = sync.network_id
        self.sync = sync
        self.camera_type = ""
        self.product_type = "catalina"
        self.status = "done"
        self.motion_enabled = True
        self.motion_detected = False
        self.battery_state = "ok"
        self.battery_level = None
        self.battery_voltage = 165
        self.temperature = 70
        self.temperature_calibrated = 68
        self.wifi_strength = -58
        self.sync_signal_strength = None
        self.last_record = None
        self.thumbnail = "https://example/thumb.jpg?ts=1"
        self._cached_image = b"JPEG-1"
        self._cached_video = None
        self.arm_calls: list[bool] = []
        self.snaps = 0
        self._pending_arm: bool | None = None

    @property
    def image_from_cache(self) -> bytes | None:
        return self._cached_image or None

    async def async_arm(self, value: bool) -> dict[str, Any]:
        self.arm_calls.append(value)
        self._pending_arm = value
        return {"id": 1}

    async def snap_picture(self) -> dict[str, Any]:
        self.snaps += 1
        return {"id": 2}

    async def update(self, _info: Any, **_kwargs: Any) -> None:
        # What Blink reports once the command has landed.
        if self._pending_arm is not None:
            self.motion_enabled = self._pending_arm
        if self.snaps:
            self.thumbnail = f"https://example/thumb.jpg?ts={1 + self.snaps}"
            self._cached_image = f"JPEG-{1 + self.snaps}".encode()


class FakeBlink:
    def __init__(self) -> None:
        sync = FakeSync("114 Cooper", "2001", armed=True)
        sync.blink = self
        self.sync = {"114 Cooper": sync}
        self.cameras = {
            "Driveway": FakeCamera("Driveway", "909058", "SERIAL-A", sync),
            "Back Door": FakeCamera("Back Door", "1022965", "SERIAL-B", sync),
        }
        self.refreshes: list[float] = []
        self.fail_with: BaseException | None = None
        self.clock: Any = None
        self.homescreens = 0
        self.auth = None

    async def refresh(self, **_kwargs: Any) -> bool:
        self.refreshes.append(self.clock.now if self.clock else 0)
        if self.fail_with is not None:
            raise self.fail_with
        for camera in self.cameras.values():
            # A clip should never be left cached between polls.
            camera._cached_video = b"clip" if camera._cached_video == b"" else None
        return True

    async def get_homescreen(self) -> None:
        self.homescreens += 1
