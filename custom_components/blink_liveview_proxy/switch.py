"""Switch platform: each Blink camera's motion detection.

Only set up with the Blink entities option on; see CONF_BLINK_ENTITIES.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ProxyError
from .blink_entity import BlinkProxyCameraEntity, action_error, runtime_cameras
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one motion-detection switch per camera."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        BlinkProxyMotionSwitch(
            runtime["coordinator"],
            runtime["client"],
            entry,
            camera,
            runtime["hub_device_id"],
        )
        for camera in runtime_cameras(hass, entry)
    )


class BlinkProxyMotionSwitch(BlinkProxyCameraEntity, SwitchEntity):
    """Motion detection on one camera, as the Blink app's per-camera toggle."""

    _entity_domain = "switch"

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:motion-sensor"

    def __init__(self, coordinator, client, entry, camera, hub_device_id) -> None:
        super().__init__(
            coordinator,
            client,
            entry,
            camera,
            hub_device_id,
            "motion_detection",
            "Motion detection",
        )

    @property
    def is_on(self) -> bool | None:
        row = self.row
        return None if row is None else row.get("motion_enabled")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        try:
            row = await self._client.async_set_motion_detection(self._slug, enabled)
        except ProxyError as err:
            raise action_error(
                f"turn motion detection {'on' if enabled else 'off'}", err
            ) from err
        self.coordinator.apply_camera_row(row)
