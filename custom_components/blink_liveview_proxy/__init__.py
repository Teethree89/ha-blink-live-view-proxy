"""Blink live-view proxy integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BlinkLiveviewProxyClient
from .const import (
    CONF_BASE_URL,
    CONF_STREAM_SECONDS,
    CONF_TOKEN,
    DEFAULT_STREAM_SECONDS,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import BlinkLiveviewProxyCoordinator
from .views import async_register_views


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Set up integration-level HTTP views."""
    async_register_views(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Blink live-view proxy from a config entry."""
    async_register_views(hass)
    merged = {**entry.data, **entry.options}
    client = BlinkLiveviewProxyClient(
        async_get_clientsession(hass),
        merged[CONF_BASE_URL],
        merged.get(CONF_TOKEN),
    )
    coordinator = BlinkLiveviewProxyCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
        "stream_seconds": int(merged.get(CONF_STREAM_SECONDS, DEFAULT_STREAM_SECONDS)),
    }
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
