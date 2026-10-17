"""Binary sensor platform for Blink live-view proxy."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .blink_devices import HEALTH_OBJECT_ID, sync_online
from .blink_entity import (
    BlinkProxyCameraEntity,
    BlinkProxySyncEntity,
    runtime_cameras,
    runtime_syncs,
)
from .const import DOMAIN
from .coordinator import BlinkLiveviewProxyCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """The proxy's health, each camera's motion and battery, each sync's connection."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator: BlinkLiveviewProxyCoordinator = runtime["coordinator"]
    entities: list[BinarySensorEntity] = [
        BlinkLiveviewProxyHealthSensor(coordinator, entry)
    ]
    for camera in runtime_cameras(hass, entry):
        for sensor_class in (BlinkProxyMotionSensor, BlinkProxyBatterySensor):
            entities.append(
                sensor_class(
                    coordinator,
                    runtime["client"],
                    entry,
                    camera,
                    runtime["hub_device_id"],
                )
            )
    for sync in runtime_syncs(hass, entry):
        entities.append(
            BlinkProxySyncConnectionSensor(
                coordinator,
                runtime["client"],
                entry,
                sync,
                runtime["hub_device_id"],
            )
        )
    async_add_entities(entities)


class BlinkLiveviewProxyHealthSensor(
    CoordinatorEntity[BlinkLiveviewProxyCoordinator], BinarySensorEntity
):
    """Represent the local proxy health endpoint."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "Blink Live View Proxy"

    def __init__(
        self, coordinator: BlinkLiveviewProxyCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_health"
        # Every example dashboard and the generators' proxy pill read
        # binary_sensor.blink_liveview_proxy, and installs from before the
        # rename keep that id in the entity registry regardless - so a fresh
        # install must land on the same one, or the same YAML works for one
        # group and not the other.
        #
        # Set outright, not suggested through the suggested_object_id property,
        # which this used to be: from Home Assistant 2026.9 that is only a base
        # and gets the device's name put in front of it, which made a fresh
        # install's sensor binary_sensor.blink_live_view_proxy_blink_liveview_proxy
        # and left every generated dashboard's pill reading nothing. An entity_id
        # set before the entity is added is taken as it is, on every release
        # this integration supports; see BlinkProxyCameraEntity.
        self.entity_id = f"binary_sensor.{HEALTH_OBJECT_ID}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Blink Live View Proxy",
            "manufacturer": "Local",
        }

    @property
    def is_on(self) -> bool:
        """Return whether the proxy is reachable."""
        health: dict[str, Any] = self.coordinator.data.get("health", {})
        return bool(health.get("ok"))


class BlinkProxyMotionSensor(BlinkProxyCameraEntity, BinarySensorEntity):
    """Motion on one camera since the proxy's previous Blink refresh.

    As coarse as the poll interval: a clip recorded between two polls shows
    as motion at the second one.
    """

    _entity_domain = "binary_sensor"

    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, coordinator, client, entry, camera, hub_device_id) -> None:
        super().__init__(
            coordinator, client, entry, camera, hub_device_id, "motion", "Motion"
        )

    @property
    def is_on(self) -> bool | None:
        row = self.row
        return None if row is None else bool(row.get("motion_detected"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"last_record": (self.row or {}).get("last_record")}


class BlinkProxyBatterySensor(BlinkProxyCameraEntity, BinarySensorEntity):
    """On when Blink calls the battery low."""

    _entity_domain = "binary_sensor"

    _attr_device_class = BinarySensorDeviceClass.BATTERY

    def __init__(self, coordinator, client, entry, camera, hub_device_id) -> None:
        super().__init__(
            coordinator, client, entry, camera, hub_device_id, "battery", "Battery"
        )

    @property
    def is_on(self) -> bool | None:
        row = self.row
        return None if row is None else row.get("battery_low")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        row = self.row or {}
        return {
            "battery_state": row.get("battery"),
            "battery_level": row.get("battery_level"),
        }


class BlinkProxySyncConnectionSensor(BlinkProxySyncEntity, BinarySensorEntity):
    """On while Blink can reach this sync module.

    The alarm panel cannot say this. Blink keeps reporting the last arm state
    for a module that has dropped off, so a system that is no longer watching
    still reads as armed. This is the entity to condition an automation on.
    """

    _entity_domain = "binary_sensor"

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, client, entry, sync, hub_device_id) -> None:
        super().__init__(
            coordinator, client, entry, sync, hub_device_id, "connection", "Connection"
        )

    @property
    def is_on(self) -> bool | None:
        return sync_online(self.row)
