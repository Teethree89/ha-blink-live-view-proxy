"""Shared base for the entities built from the proxy's Blink session.

The proxy's session provides everything the camera and sync module entities
need, so Home Assistant's own Blink integration is not used at all. See
proxy/blink_proxy/devices.py for why that matters.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import BlinkLiveviewProxyClient, ProxyError
from .blink_devices import (
    camera_key,
    camera_object_id,
    camera_unique_id,
    devices_cameras,
    devices_syncs,
    find_camera,
    find_sync,
    poll_failed,
    sync_device_key,
    sync_object_id,
    sync_unique_id,
)
from .const import DOMAIN
from .coordinator import BlinkLiveviewProxyCoordinator
from .device_parent import parent_device_info

LOGGER = logging.getLogger(__name__)

SNAPSHOT_SUFFIX = "snapshot"


def runtime_cameras(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    coordinator: BlinkLiveviewProxyCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    cameras = devices_cameras((coordinator.data or {}).get("devices"))
    if not cameras:
        LOGGER.warning(
            "The proxy reported no Blink device state, so no snapshot, motion, "
            "battery or alarm entities were created. Update the proxy if it is "
            "older than this integration, then reload."
        )
    return cameras


def runtime_syncs(hass: HomeAssistant, entry: ConfigEntry) -> list[dict[str, Any]]:
    coordinator: BlinkLiveviewProxyCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    return devices_syncs((coordinator.data or {}).get("devices"))


def snapshot_entity_id(
    hass: HomeAssistant, entry_id: str, camera: dict[str, Any]
) -> str | None:
    """The entity id of this integration's own snapshot camera for `camera`.

    Looked up by unique id, so a user who renamed the entity still gets theirs.
    """
    return er.async_get(hass).async_get_entity_id(
        "camera", DOMAIN, camera_unique_id(entry_id, camera, SNAPSHOT_SUFFIX)
    )




class BlinkProxyCameraEntity(CoordinatorEntity[BlinkLiveviewProxyCoordinator]):
    """One entity of one Blink camera, on the device the live camera made.

    The entity id is set outright rather than suggested through the
    suggested_object_id property: from Home Assistant 2026.9 that is only a
    base, prefixed with the device name, so "blink_proxy_driveway" came out as
    "blink_driveway_blink_proxy_driveway". An entity_id set before the entity
    is added is taken as it is, on every release this integration supports.
    Each platform names its domain in _entity_domain.
    """

    _attr_has_entity_name = False
    _entity_domain: str

    def __init__(
        self,
        coordinator: BlinkLiveviewProxyCoordinator,
        client: BlinkLiveviewProxyClient,
        entry: ConfigEntry,
        camera: dict[str, Any],
        hub_device_id: str,
        suffix: str,
        name_suffix: str = "",
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._slug = str(camera.get("slug") or "")
        self._suffix = suffix
        name = str(camera.get("name") or self._slug.replace("_", " ").title())
        self._attr_name = f"Blink {name} {name_suffix}".strip()
        self._attr_unique_id = camera_unique_id(entry.entry_id, camera, suffix)
        self.entity_id = f"{self._entity_domain}." + camera_object_id(
            self._slug, "" if suffix == SNAPSHOT_SUFFIX else suffix
        )
        self._attr_device_info = {
            "identifiers": {(DOMAIN, camera_key(camera))},
            "name": f"Blink {name}",
            "manufacturer": "Blink",
            "model": camera.get("product_type"),
            **parent_device_info(hub_device_id, (DOMAIN, entry.entry_id)),
        }

    @property
    def row(self) -> dict[str, Any] | None:
        return find_camera((self.coordinator.data or {}).get("devices"), self._slug)

    @property
    def available(self) -> bool:
        devices = (self.coordinator.data or {}).get("devices")
        return (
            super().available
            and self.row is not None
            and not poll_failed(devices)
        )


class BlinkProxySyncEntity(CoordinatorEntity[BlinkLiveviewProxyCoordinator]):
    """One entity of one Blink sync module. Ids as BlinkProxyCameraEntity."""

    _attr_has_entity_name = False
    _entity_domain: str

    def __init__(
        self,
        coordinator: BlinkLiveviewProxyCoordinator,
        client: BlinkLiveviewProxyClient,
        entry: ConfigEntry,
        sync: dict[str, Any],
        hub_device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._network_id = str(sync.get("network_id"))
        name = str(sync.get("name") or self._network_id)
        self._attr_name = f"Blink {name}"
        self._attr_unique_id = sync_unique_id(entry.entry_id, sync)
        self.entity_id = f"{self._entity_domain}.{sync_object_id(sync)}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, sync_device_key(sync))},
            "name": f"Blink {name}",
            "manufacturer": "Blink",
            "model": "Sync Module",
            "serial_number": sync.get("serial"),
            **parent_device_info(hub_device_id, (DOMAIN, entry.entry_id)),
        }

    @property
    def row(self) -> dict[str, Any] | None:
        return find_sync((self.coordinator.data or {}).get("devices"), self._network_id)

    @property
    def available(self) -> bool:
        devices = (self.coordinator.data or {}).get("devices")
        return (
            super().available
            and self.row is not None
            and not poll_failed(devices)
        )


def action_error(what: str, err: ProxyError) -> HomeAssistantError:
    """A failed Blink command, said in terms of what was asked for."""
    return HomeAssistantError(f"Blink did not {what}: {err}")
