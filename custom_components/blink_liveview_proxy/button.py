"""Button platform: take a new snapshot on each Blink camera.

Only set up with the Blink entities option on; see CONF_BLINK_ENTITIES. The
official integration offered this as the blink.trigger_camera action.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Set up one snapshot button per camera."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        BlinkProxySnapshotButton(
            runtime["coordinator"],
            runtime["client"],
            entry,
            camera,
            runtime["hub_device_id"],
        )
        for camera in runtime_cameras(hass, entry)
    )


class BlinkProxySnapshotButton(BlinkProxyCameraEntity, ButtonEntity):
    """Ask the camera for a new picture. It wakes the camera, so battery."""

    _entity_domain = "button"

    _attr_icon = "mdi:camera-retake"

    def __init__(self, coordinator, client, entry, camera, hub_device_id) -> None:
        super().__init__(
            coordinator,
            client,
            entry,
            camera,
            hub_device_id,
            "refresh_snapshot",
            "Refresh snapshot",
        )

    async def async_press(self) -> None:
        try:
            row = await self._client.async_snap_camera(self._slug)
        except ProxyError as err:
            raise action_error("take a new snapshot", err) from err
        self.coordinator.apply_camera_row(row)
