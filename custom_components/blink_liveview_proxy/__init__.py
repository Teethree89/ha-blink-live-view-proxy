"""Blink live-view proxy integration."""

from __future__ import annotations

import logging

from homeassistant.components import panel_custom
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

LOGGER = logging.getLogger(__name__)

FRONTEND_RESOURCE_URL = "/api/blink_liveview_proxy/static/blink-liveview-dialog.js"
AUTH_PANEL_MODULE_URL = "/api/blink_liveview_proxy/static/blink-proxy-auth-panel.js"
AUTH_PANEL_PATH = "blink-liveview-proxy-auth"


async def _async_register_auth_panel(hass: HomeAssistant) -> None:
    """Register the admin dashboard while preserving its original URL."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("_auth_panel_registered"):
        return
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=AUTH_PANEL_PATH,
        webcomponent_name="blink-proxy-auth-panel",
        sidebar_title="Blink Proxy",
        sidebar_icon="mdi:cctv",
        module_url=AUTH_PANEL_MODULE_URL,
        require_admin=True,
        config_panel_domain=DOMAIN,
    )
    domain_data["_auth_panel_registered"] = True


async def _async_register_frontend_resource(hass: HomeAssistant) -> None:
    """Add the dialog module to Lovelace's resources if it is not there.

    Without this the card's fire-dom-event payload goes out and nothing is
    listening: no console error, no log line, no failed request in the network
    tab. The tile just sits there. Registering it by hand was the entire fix,
    and more than one person lost a day to finding that out.

    Only storage-mode Lovelace can be written to. In YAML mode the resource
    list comes from configuration.yaml and is read-only, so say what to add
    rather than failing.
    """
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        LOGGER.debug("Lovelace is not set up; skipping resource registration")
        return

    resources = getattr(lovelace, "resources", None)
    if resources is None:
        return

    if getattr(lovelace, "resource_mode", None) != "storage":
        LOGGER.warning(
            "Lovelace is in YAML mode, so the dialog resource cannot be added "
            "automatically. Add this to your resources, or live view, clips "
            "and snapshot buttons will do nothing when tapped: %s",
            FRONTEND_RESOURCE_URL,
        )
        return

    try:
        # Storage-backed resources are lazy; async_get_info loads them.
        await resources.async_get_info()
        for item in resources.async_items() or []:
            # Existing entries may carry a cache-busting query string.
            if str(item.get("url", "")).split("?", 1)[0] == FRONTEND_RESOURCE_URL:
                return
        await resources.async_create_item(
            {"res_type": "module", "url": FRONTEND_RESOURCE_URL}
        )
    except Exception:  # noqa: BLE001 - never block setup over a dashboard nicety
        LOGGER.exception(
            "Could not register the dialog resource automatically. Add it by "
            "hand under Settings > Dashboards > Resources as a JavaScript "
            "module: %s",
            FRONTEND_RESOURCE_URL,
        )
        return

    LOGGER.info("Registered the Lovelace resource %s", FRONTEND_RESOURCE_URL)


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Set up integration-level HTTP views."""
    async_register_views(hass)
    await _async_register_auth_panel(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Blink live-view proxy from a config entry."""
    async_register_views(hass)
    await _async_register_auth_panel(hass)
    await _async_register_frontend_resource(hass)
    merged = {**entry.data, **entry.options}
    client = BlinkLiveviewProxyClient(
        async_get_clientsession(hass),
        merged[CONF_BASE_URL],
        merged.get(CONF_TOKEN),
    )
    hass.data.setdefault(DOMAIN, {}).setdefault("_auth_clients", {})[
        entry.entry_id
    ] = client
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
        hass.data[DOMAIN].get("_auth_clients", {}).pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
