"""Coordinator for the Blink live-view proxy integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.loader import async_get_integration

from .api import BlinkLiveviewProxyClient, ProxyAuthError, ProxyConnectionError
from .const import CONF_BASE_URL, DEFAULT_SCAN_INTERVAL, DOMAIN, MINIMUM_PROXY_VERSION
from .version_check import (
    NOTICE_OUTDATED,
    NOTICE_OUTDATED_FIXABLE,
    can_start_update,
    describe,
    infer_version,
    review,
    update_blocker,
)

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
        # Two notices, never both at once: one for a proxy too old to do
        # what is asked of it, one for a proxy that merely trails this
        # release. They read differently and clear on different events, so
        # they cannot share an id.
        self._version_issue_id = f"proxy_outdated_{entry.entry_id}"
        self._behind_issue_id = f"proxy_behind_{entry.entry_id}"
        self._own_version: str | None = None

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

        self._review_proxy_version(
            infer_version(status), await self._async_own_version(), status
        )
        return {"health": health, "status": status, "cameras": cameras}

    async def _async_own_version(self) -> str | None:
        """This integration's own version, read once and kept.

        The manifest is the only place that knows it, and a release moves both
        halves together, so "older than me" is the honest question to ask of
        the proxy - rather than the hand-set floor, which only says whether
        anything is actually broken.
        """
        if self._own_version is None:
            integration = await async_get_integration(self.hass, DOMAIN)
            self._own_version = str(integration.version or "")
        return self._own_version or None

    def _review_proxy_version(
        self,
        reported: str | None,
        own_version: str | None,
        status: dict[str, Any] | None,
    ) -> None:
        """Raise, or clear, the notice that the proxy needs updating.

        Home Assistant cannot reach onto another host by itself, so what is
        offered depends on what the proxy says it can do. A systemd install
        runs its own updater and the add-on has Supervisor: both get a Fix
        button. Anything else gets the same notice with instructions, because a
        button that cannot work is worse than a paragraph that can be read.

        Either notice clears itself on the next poll after the proxy reports a
        newer version, without anyone reloading the integration.
        """
        entry = self.config_entry
        fixable = can_start_update(status)
        notice = review(reported, own_version, MINIMUM_PROXY_VERSION, status)

        if notice is None:
            ir.async_delete_issue(self.hass, DOMAIN, self._version_issue_id)
            ir.async_delete_issue(self.hass, DOMAIN, self._behind_issue_id)
            return

        outdated = notice == NOTICE_OUTDATED
        if not fixable:
            LOGGER.debug(
                "Proxy at %s is behind and cannot update itself: %s",
                entry.data.get(CONF_BASE_URL, "the proxy"),
                update_blocker(status) or "it did not say why",
            )

        issue_id = self._version_issue_id if outdated else self._behind_issue_id
        stale_id = self._behind_issue_id if outdated else self._version_issue_id
        ir.async_delete_issue(self.hass, DOMAIN, stale_id)

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=fixable,
            severity=ir.IssueSeverity.WARNING,
            translation_key=(
                NOTICE_OUTDATED_FIXABLE if outdated and fixable else notice
            ),
            translation_placeholders={
                "base_url": entry.data.get(CONF_BASE_URL, "the proxy"),
                "proxy_version": describe(reported),
                "minimum_version": MINIMUM_PROXY_VERSION,
                "integration_version": own_version or "a newer release",
            },
            # The flow has to find its way back to this entry, and an issue id
            # is the only thing it is handed.
            data={"entry_id": entry.entry_id},
            learn_more_url=DOCS_URL,
        )
