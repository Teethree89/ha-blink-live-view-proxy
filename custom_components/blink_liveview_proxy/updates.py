"""Start proxy updates from Repairs or the admin panel."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .api import ProxyAuthError, ProxyConnectionError
from .const import DOMAIN
from .version_check import (
    UPDATE_METHOD_SUPERVISOR,
    can_start_update,
    infer_version,
    update_method,
)

LOGGER = logging.getLogger(__name__)

ABORT_ALREADY_RUNNING = "already_running"
ABORT_ENTRY_GONE = "entry_gone"
ABORT_NO_ADDON = "no_addon"
ABORT_NOT_SUPPORTED = "not_supported"
ABORT_UPDATE_FAILED = "update_failed"


class UpdateAborted(Exception):
    """An update could not be started for a safe, presentable reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def runtime(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Return one config entry's runtime, or an empty mapping."""
    value = hass.data.get(DOMAIN, {}).get(entry_id)
    return value if isinstance(value, dict) else {}


def status(hass: HomeAssistant, entry_id: str) -> dict[str, Any] | None:
    """Return the last proxy status for an entry."""
    coordinator = runtime(hass, entry_id).get("coordinator")
    value = (coordinator.data or {}).get("status") if coordinator else None
    return value if isinstance(value, dict) else None


async def async_start_update(hass: HomeAssistant, entry_id: str) -> None:
    """Start the update route advertised by this install."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        raise UpdateAborted(ABORT_ENTRY_GONE)

    current_status = status(hass, entry_id)
    if not can_start_update(current_status):
        raise UpdateAborted(ABORT_NOT_SUPPORTED)
    if update_method(current_status) == UPDATE_METHOD_SUPERVISOR:
        await _async_update_addon(hass, current_status)
        return

    coordinator = runtime(hass, entry_id).get("coordinator")
    if coordinator is None:
        raise UpdateAborted(ABORT_ENTRY_GONE)

    try:
        await coordinator.client.async_start_proxy_update()
    except ProxyAuthError as err:
        LOGGER.error("Proxy rejected the token when asked to update: %s", err)
        raise UpdateAborted(ABORT_UPDATE_FAILED) from err
    except ProxyConnectionError as err:
        if err.status == 409:
            raise UpdateAborted(ABORT_ALREADY_RUNNING) from err
        if err.status == 501:
            raise UpdateAborted(ABORT_NOT_SUPPORTED) from err
        LOGGER.error("Asking the proxy to update itself failed: %s", err)
        raise UpdateAborted(ABORT_UPDATE_FAILED) from err


async def _async_update_addon(
    hass: HomeAssistant, current_status: dict[str, Any] | None
) -> None:
    """Hand an add-on update to Supervisor across supported HA releases."""
    try:
        from homeassistant.components.hassio import get_addons_info
    except ImportError as err:  # pragma: no cover - hassio ships with core
        LOGGER.error("Supervisor helpers are unavailable: %s", err)
        raise UpdateAborted(ABORT_NOT_SUPPORTED) from err

    try:
        addons = get_addons_info(hass) or {}
    except Exception as err:  # noqa: BLE001 - version-specific HA errors
        LOGGER.error("Supervisor add-on inventory is unavailable: %s", err)
        raise UpdateAborted(ABORT_UPDATE_FAILED) from err

    slug = _addon_slug(addons)
    if slug is None:
        LOGGER.error("Supervisor lists no add-on matching %s", DOMAIN)
        raise UpdateAborted(ABORT_NO_ADDON)

    try:
        try:
            from homeassistant.components.hassio.update_helper import update_addon
        except ImportError:
            from homeassistant.components.hassio import async_update_addon

            await async_update_addon(hass, slug, backup=False)
        else:
            await update_addon(
                hass,
                slug,
                False,
                "Blink Live View Proxy",
                infer_version(current_status),
            )
    except Exception as err:  # noqa: BLE001 - Supervisor raises its own types
        LOGGER.exception("Supervisor could not update add-on %s", slug)
        raise UpdateAborted(ABORT_UPDATE_FAILED) from err


def _addon_slug(addons: dict) -> str | None:
    """Find this add-on despite Supervisor's repository slug prefix."""
    for candidate in addons:
        slug = str(candidate or "")
        if slug == DOMAIN or slug.endswith(f"_{DOMAIN}"):
            return slug
    return None
