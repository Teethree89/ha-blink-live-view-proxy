"""Coordinator for the Blink live-view proxy integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BlinkLiveviewProxyClient, ProxyAuthError, ProxyConnectionError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

LOGGER = logging.getLogger(__name__)


class BlinkLiveviewProxyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll the local proxy for health and camera inventory."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: BlinkLiveviewProxyClient,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=DEFAULT_SCAN_INTERVAL,
            always_update=False,
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch current proxy data."""
        try:
            health = await self.client.async_get_health()
            cameras = await self.client.async_get_cameras()
        except ProxyAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ProxyConnectionError as err:
            raise UpdateFailed(str(err)) from err

        return {"health": health, "cameras": cameras}
