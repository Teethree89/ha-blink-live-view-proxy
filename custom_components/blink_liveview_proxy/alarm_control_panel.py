"""Alarm panel platform: arm and disarm each Blink sync module.

Built from the proxy's own Blink session; see proxy/blink_proxy/devices.py.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ProxyError
from .blink_devices import alarm_state
from .blink_entity import BlinkProxySyncEntity, action_error, runtime_syncs
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one alarm panel per sync module."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        BlinkProxySyncAlarm(
            runtime["coordinator"],
            runtime["client"],
            entry,
            sync,
            runtime["hub_device_id"],
        )
        for sync in runtime_syncs(hass, entry)
    )


class BlinkProxySyncAlarm(BlinkProxySyncEntity, AlarmControlPanelEntity):
    """The sync module's system arm, the same switch the Blink app shows.

    Blink has one armed state, so it maps to armed_away. No code: Blink has
    none to check.
    """

    _entity_domain = "alarm_control_panel"

    _attr_supported_features = AlarmControlPanelEntityFeature.ARM_AWAY
    _attr_code_arm_required = False
    _attr_icon = "mdi:security"

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        state = alarm_state(self.row)
        return None if state is None else AlarmControlPanelState(state)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Blink's word on the module itself, which the state cannot carry.

        An unreachable module keeps its last arm state here, so anything that
        needs to know the system is really watching reads this, or the
        connection sensor beside it.
        """
        return {"status": (self.row or {}).get("status")}

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._async_set(True)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._async_set(False)

    async def _async_set(self, armed: bool) -> None:
        try:
            row = await self._client.async_set_sync_armed(self._network_id, armed)
        except ProxyError as err:
            raise action_error("arm" if armed else "disarm", err) from err
        self.coordinator.apply_sync_row(row)
