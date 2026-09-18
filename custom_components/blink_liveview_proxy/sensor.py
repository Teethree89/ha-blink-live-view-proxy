"""Sensor platform: temperature, Wi-Fi signal and battery voltage per camera,
plus when the proxy last refreshed Blink.

Only set up with the Blink entities option on; see CONF_BLINK_ENTITIES.
"""

from __future__ import annotations

import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .blink_devices import CAMERA_SENSORS, sensor_value
from .blink_entity import BlinkProxyCameraEntity, runtime_cameras
from .const import DOMAIN
from .coordinator import BlinkLiveviewProxyCoordinator

# kind -> (device class, unit, state class, category)
_KINDS: dict[str, tuple[Any, Any, Any, Any]] = {
    "temperature_f": (
        SensorDeviceClass.TEMPERATURE,
        UnitOfTemperature.FAHRENHEIT,
        SensorStateClass.MEASUREMENT,
        None,
    ),
    "signal_dbm": (
        SensorDeviceClass.SIGNAL_STRENGTH,
        SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        SensorStateClass.MEASUREMENT,
        EntityCategory.DIAGNOSTIC,
    ),
    "voltage": (
        SensorDeviceClass.VOLTAGE,
        UnitOfElectricPotential.VOLT,
        SensorStateClass.MEASUREMENT,
        EntityCategory.DIAGNOSTIC,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the camera sensors and the proxy's poll sensor."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        BlinkProxyPollSensor(runtime["coordinator"], entry)
    ]
    for camera in runtime_cameras(hass, entry):
        for suffix, label, field, kind in CAMERA_SENSORS:
            # Not every model reports every value. A sensor that could only
            # ever say "unknown" is left out rather than created.
            if sensor_value(camera, field) is None:
                continue
            entities.append(
                BlinkProxyCameraSensor(
                    runtime["coordinator"],
                    runtime["client"],
                    entry,
                    camera,
                    runtime["hub_device_id"],
                    suffix,
                    label,
                    field,
                    kind,
                )
            )
    async_add_entities(entities)


class BlinkProxyCameraSensor(BlinkProxyCameraEntity, SensorEntity):
    """One numeric reading from one camera."""

    _entity_domain = "sensor"

    def __init__(
        self,
        coordinator,
        client,
        entry,
        camera,
        hub_device_id,
        suffix: str,
        label: str,
        field: str,
        kind: str,
    ) -> None:
        super().__init__(
            coordinator, client, entry, camera, hub_device_id, suffix, label
        )
        self._field = field
        device_class, unit, state_class, category = _KINDS[kind]
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = state_class
        self._attr_entity_category = category

    @property
    def native_value(self) -> Any:
        return sensor_value(self.row, self._field)


class BlinkProxyPollSensor(
    CoordinatorEntity[BlinkLiveviewProxyCoordinator], SensorEntity
):
    """When the proxy last refreshed Blink, and how the poll is doing.

    The one place a failing poll shows up without reading the proxy's log:
    the state stops moving, and the attributes say why.
    """

    _attr_has_entity_name = False
    _attr_name = "Blink Live View Proxy last Blink refresh"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-refresh"

    def __init__(
        self, coordinator: BlinkLiveviewProxyCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_blink_poll"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}
        # Set, not suggested; see BlinkProxyCameraEntity.
        self.entity_id = "sensor.blink_liveview_proxy_last_blink_refresh"

    @property
    def _poll(self) -> dict[str, Any]:
        devices = (self.coordinator.data or {}).get("devices") or {}
        return devices.get("poll") or {}

    @property
    def native_value(self) -> datetime.datetime | None:
        stamp = self._poll.get("last_success")
        if not isinstance(stamp, (int, float)):
            return None
        return datetime.datetime.fromtimestamp(stamp, tz=datetime.timezone.utc)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        poll = self._poll
        return {
            key: poll.get(key)
            for key in ("state", "interval", "failures", "polls", "last_error")
        }
