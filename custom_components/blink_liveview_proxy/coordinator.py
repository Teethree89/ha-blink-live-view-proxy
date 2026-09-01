"""Coordinator for the Blink live-view proxy integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BlinkLiveviewProxyClient, ProxyAuthError, ProxyConnectionError
from .const import CONF_BASE_URL, DEFAULT_SCAN_INTERVAL, DOMAIN, MINIMUM_PROXY_VERSION
from .version_check import describe, infer_version, is_outdated

DOCS_URL = (
    "https://github.com/Teethree89/ha-blink-live-view-proxy"
    "/blob/main/docs/OPERATIONS.md#if-the-panel-will-not-start-a-login"
)

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
        self._version_issue_id = f"proxy_outdated_{entry.entry_id}"

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch current proxy data."""
        try:
            health = await self.client.async_get_health()
            status = await self.client.async_get_status()
            cameras = await self.client.async_get_cameras()
        except ProxyAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ProxyConnectionError as err:
            raise UpdateFailed(str(err)) from err

        self._review_proxy_version(infer_version(status))
        return {"health": health, "status": status, "cameras": cameras}

    def _review_proxy_version(self, reported: str | None) -> None:
        """Raise, or clear, the notice that the proxy is too old.

        Home Assistant cannot upgrade a service on another host, so this says
        what is wrong and what to run. It clears itself on the next poll after
        the proxy is upgraded, without anyone reloading the integration.
        """
        entry = self.config_entry
        if not is_outdated(reported, MINIMUM_PROXY_VERSION):
            ir.async_delete_issue(self.hass, DOMAIN, self._version_issue_id)
            return

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._version_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="proxy_outdated",
            translation_placeholders={
                "base_url": entry.data.get(CONF_BASE_URL, "the proxy"),
                "proxy_version": describe(reported),
                "minimum_version": MINIMUM_PROXY_VERSION,
            },
            learn_more_url=DOCS_URL,
        )
