"""Binary sensor platform for Blink live-view proxy."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BlinkLiveviewProxyCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up proxy health binary sensor."""
    coordinator: BlinkLiveviewProxyCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    async_add_entities([BlinkLiveviewProxyHealthSensor(coordinator, entry)])


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
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Blink Live View Proxy",
            "manufacturer": "Local",
        }

    @property
    def suggested_object_id(self) -> str | None:
        """Keep binary_sensor.blink_liveview_proxy through the rename.

        Home Assistant derives a new entity's object id from its name, and the
        name is now "Blink Live View Proxy". Every example dashboard and the
        generator's proxy pill read binary_sensor.blink_liveview_proxy, and
        installs from before the rename keep that id in the entity registry
        regardless - so a fresh install must land on the same one, or the
        same YAML works for one group and not the other.
        """
        return "blink_liveview_proxy"

    @property
    def is_on(self) -> bool:
        """Return whether the proxy is reachable."""
        health: dict[str, Any] = self.coordinator.data.get("health", {})
        return bool(health.get("ok"))
