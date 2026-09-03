"""Blink live-view proxy integration."""

from __future__ import annotations

import logging

from typing import Any

from homeassistant.components import panel_custom
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later

from .api import BlinkLiveviewProxyClient
from .const import (
    ASSET_URL_BASE,
    CONF_BASE_URL,
    CONF_STREAM_SECONDS,
    CONF_TOKEN,
    DEFAULT_STREAM_SECONDS,
    DOMAIN,
    FRONTEND_RESOURCE_URL,
    LEGACY_FRONTEND_RESOURCE_URL,
    PLATFORMS,
)
from .coordinator import BlinkLiveviewProxyCoordinator
from .lovelace import is_writable, resource_collection
from .views import async_register_views

LOGGER = logging.getLogger(__name__)

# How long to wait before a second registration attempt when Lovelace was not
# reachable on a running instance. Long enough for a slow start, short enough
# that nobody is staring at a dead dashboard while it elapses.
RESOURCE_RETRY_SECONDS = 30

AUTH_PANEL_MODULE_URL = f"{ASSET_URL_BASE}/blink-proxy-auth-panel.js"
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


async def _async_try_register_resource(hass: HomeAssistant) -> bool:
    """Put the dialog module in Lovelace's resources. False if it could not.

    Without this the card's fire-dom-event payload goes out and nothing is
    listening: no console error, no log line, no failed request in the network
    tab. The tile just sits there. Registering it by hand was the entire fix,
    and more than one person lost a day to finding that out.

    False means only "Lovelace could not be reached", which is a retryable
    state, not a failure. Everything else - YAML mode, an existing entry, a
    write that raised - is a final answer and returns True.

    Both Lovelace questions go through lovelace.py because the answers moved
    twice: reading them directly found nothing below 2026.3, so this returned
    early every time and the registration it promises never happened.
    """
    lovelace = hass.data.get("lovelace")
    resources = resource_collection(lovelace)
    if resources is None:
        return False

    if not is_writable(lovelace):
        # YAML mode's resource list comes from configuration.yaml and cannot be
        # written to. Say what to add rather than failing. The legacy path is
        # still served, so anyone already pointing at it needs to do nothing.
        LOGGER.warning(
            "Lovelace is in YAML mode, so the dialog resource cannot be added "
            "automatically. Add this to your resources, or live view, clips "
            "and snapshot buttons will do nothing when tapped: %s",
            FRONTEND_RESOURCE_URL,
        )
        return True

    try:
        # Storage-backed resources are lazy; async_get_info loads them.
        await resources.async_get_info()
        legacy: dict | None = None
        for item in resources.async_items() or []:
            # Existing entries may carry a cache-busting query string.
            url = str(item.get("url", "")).split("?", 1)[0]
            if url == FRONTEND_RESOURCE_URL:
                return True
            if url == LEGACY_FRONTEND_RESOURCE_URL:
                legacy = item

        if legacy is not None and legacy.get("id"):
            # Rewrite in place. Adding the new URL alongside would load the
            # module twice and leave a stale entry nobody knows to remove -
            # and the stale one is the one the service worker holds forever.
            await resources.async_update_item(
                legacy["id"], {"url": FRONTEND_RESOURCE_URL}
            )
            LOGGER.info(
                "Moved the Lovelace resource off the cached path: %s -> %s",
                LEGACY_FRONTEND_RESOURCE_URL,
                FRONTEND_RESOURCE_URL,
            )
            return True

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
        return True

    LOGGER.info("Registered the Lovelace resource %s", FRONTEND_RESOURCE_URL)
    return True


async def _async_register_frontend_resource(hass: HomeAssistant) -> None:
    """Register the dialog resource, and try again if Lovelace is not up yet.

    after_dependencies lists lovelace, which orders this correctly in the
    common case but does not guarantee it. Giving up after one attempt meant a
    boot that lost the race registered nothing at all, and said so only at
    debug level - so the symptom was a dashboard whose buttons did nothing and
    a log with no clue in it.
    """
    if await _async_try_register_resource(hass):
        return

    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("_resource_retry_armed"):
        return
    domain_data["_resource_retry_armed"] = True

    async def _retry(_now: Any) -> None:
        domain_data["_resource_retry_armed"] = False
        if not await _async_try_register_resource(hass):
            LOGGER.warning(
                "Lovelace never became reachable, so the dialog resource could "
                "not be registered. Add it by hand under Settings > Dashboards "
                "> Resources as a JavaScript module, or live view, clips and "
                "snapshot buttons will do nothing when tapped: %s",
                FRONTEND_RESOURCE_URL,
            )

    if hass.is_running:
        # Added or reloaded on a running instance, so there is no start event
        # left to wait for. Lovelace is almost certainly up and the first
        # attempt succeeded; this covers the case where it is still loading.
        async_call_later(hass, RESOURCE_RETRY_SECONDS, _retry)
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _retry)


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
