"""Config flow for the Blink live-view proxy integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .api import (
    BlinkLiveviewProxyClient,
    ProxyAuthError,
    ProxyConnectionError,
    normalize_base_url,
)
from .const import (
    ADDON_BASE_URL,
    CONF_BASE_URL,
    CONF_STREAM_SECONDS,
    CONF_TOKEN,
    DEFAULT_BASE_URL,
    DEFAULT_STREAM_SECONDS,
    DOMAIN,
    TOKEN_HANDOFF_FILE,
    URL_HANDOFF_FILE,
)
from .supervisor import addon_internal_url, addon_slug

LOGGER = logging.getLogger(__name__)

# How long one candidate address gets to answer /health while the form is
# being built. The failures that matter here — no such host, connection
# refused — come back in milliseconds; this only bounds the case where
# something swallows the packets, and nobody should wait on the API's full
# ten seconds per candidate to be shown a form they can edit.
PROBE_TIMEOUT = 4


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the setup/options schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_BASE_URL,
                default=defaults.get(CONF_BASE_URL, DEFAULT_BASE_URL),
            ): str,
            vol.Optional(
                CONF_TOKEN,
                default=defaults.get(CONF_TOKEN, ""),
            ): str,
            vol.Optional(
                CONF_STREAM_SECONDS,
                default=defaults.get(CONF_STREAM_SECONDS, DEFAULT_STREAM_SECONDS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10,
                    max=300,
                    step=5,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
        }
    )


def _read_handoff(path: str) -> str:
    """Read one of the files the add-on writes into the config directory."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def _async_handoff_token(hass: HomeAssistant) -> str:
    """Return the add-on's shared token, so nobody has to copy one by hand."""
    return await hass.async_add_executor_job(
        _read_handoff, hass.config.path(TOKEN_HANDOFF_FILE)
    )


async def _async_handoff_url(hass: HomeAssistant) -> str:
    """Return the address the add-on says it is reachable on."""
    shared = await hass.async_add_executor_job(
        _read_handoff, hass.config.path(URL_HANDOFF_FILE)
    )
    try:
        return normalize_base_url(shared)
    except ProxyConnectionError:
        return ""


def _supervisor_addon_url(hass: HomeAssistant) -> str:
    """Derive the add-on's internal address by asking Supervisor for its slug.

    Stays on the event loop on purpose: this reads Supervisor's inventory out
    of hass.data, which is a callback-side read and not an executor's to make.
    """
    try:
        from homeassistant.components.hassio import get_addons_info

        addons = get_addons_info(hass) or {}
    except Exception:  # noqa: BLE001 - no Supervisor here, or it moved this helper
        return ""
    return addon_internal_url(addon_slug(addons, DOMAIN))


async def _async_answers(hass: HomeAssistant, base_url: str, token: str) -> bool:
    """Report whether anything is listening on an address."""
    client = BlinkLiveviewProxyClient(async_get_clientsession(hass), base_url, token)
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            await client.async_get_health()
    except ProxyAuthError:
        # Something answered. Whether it likes the token is the next screen's
        # problem, not a reason to fill in a different address.
        return True
    except Exception:  # noqa: BLE001 - any failure means "try the next address"
        return False
    return True


async def _async_addon_base_url(hass: HomeAssistant, token: str) -> str:
    """Pick the address that reaches the add-on, best candidate first.

    The add-on publishes no host port by default, so the old
    homeassistant.local:8088 default reached nothing on a stock install and
    setup failed with cannot_connect while the add-on sat there working. Ask
    the add-on first, derive the Supervisor hostname second, and keep the host
    address only as the fallback it should always have been.
    """
    candidates = [
        await _async_handoff_url(hass),
        _supervisor_addon_url(hass),
        ADDON_BASE_URL,
    ]
    seen: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.append(candidate)

    for candidate in seen:
        if await _async_answers(hass, candidate, token):
            return candidate
    # Nothing answered — the add-on may simply be starting. Offer the best
    # guess rather than an address that is known not to work.
    return seen[0]


async def _validate_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Validate the proxy URL and token."""
    client = BlinkLiveviewProxyClient(
        async_get_clientsession(hass), data[CONF_BASE_URL], data.get(CONF_TOKEN)
    )
    await client.async_get_health()
    try:
        # New proxies expose this token-protected control endpoint even when
        # camera discovery is waiting for first login. That lets the config
        # entry (and its admin-only auth panel) exist before Blink is ready.
        await client.async_get_auth_status()
    except ProxyAuthError:
        raise
    except ProxyConnectionError:
        # Compatibility with older proxies that do not have browser auth.
        await client.async_get_cameras()


class BlinkLiveviewProxyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Blink live-view proxy config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                data = {
                    CONF_BASE_URL: normalize_base_url(user_input[CONF_BASE_URL]),
                    CONF_TOKEN: user_input.get(CONF_TOKEN, "").strip(),
                    CONF_STREAM_SECONDS: user_input.get(
                        CONF_STREAM_SECONDS, DEFAULT_STREAM_SECONDS
                    ),
                }
            except ProxyConnectionError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(data[CONF_BASE_URL])
                self._abort_if_unique_id_configured()

                try:
                    await _validate_input(self.hass, data)
                except ProxyAuthError:
                    errors["base"] = "invalid_auth"
                except ProxyConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:  # noqa: BLE001
                    LOGGER.exception("Unexpected Blink live-view proxy setup error")
                    errors["base"] = "unknown"
                else:
                    return self.async_create_entry(
                        title="Blink Live View Proxy",
                        data=data,
                    )

        defaults: dict[str, Any] = dict(user_input or {})
        if user_input is None:
            # An add-on install leaves its generated token in the config
            # directory. Pre-fill from it so setup is one click, not a copy.
            token = await _async_handoff_token(self.hass)
            if token:
                defaults = {
                    CONF_TOKEN: token,
                    CONF_BASE_URL: await _async_addon_base_url(self.hass, token),
                }

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(defaults),
            errors=errors,
        )

    async def async_step_reauth(self, _entry_data: Mapping[str, Any]) -> FlowResult:
        """Start reauth after the proxy rejects the stored token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Take a new proxy token, defaulting to whatever the add-on shared."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="reauth_failed")

        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_TOKEN: user_input.get(CONF_TOKEN, "").strip()}
            try:
                await _validate_input(self.hass, data)
            except ProxyAuthError:
                errors["base"] = "invalid_auth"
            except ProxyConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected Blink live-view proxy reauth error")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(entry, data=data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        shared = await _async_handoff_token(self.hass)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_TOKEN,
                        default=shared or entry.data.get(CONF_TOKEN, ""),
                    ): str
                }
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        _config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return BlinkLiveviewProxyOptionsFlow()


class BlinkLiveviewProxyOptionsFlow(config_entries.OptionsFlow):
    """Allow proxy URL/token changes from the UI."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle options."""
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            try:
                data = {
                    CONF_BASE_URL: normalize_base_url(user_input[CONF_BASE_URL]),
                    CONF_TOKEN: user_input.get(CONF_TOKEN, "").strip(),
                    CONF_STREAM_SECONDS: user_input.get(
                        CONF_STREAM_SECONDS, DEFAULT_STREAM_SECONDS
                    ),
                }
                await _validate_input(self.hass, data)
            except ProxyAuthError:
                errors["base"] = "invalid_auth"
            except ProxyConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected Blink live-view proxy options error")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title="", data=data)

        return self.async_show_form(
            step_id="init",
            data_schema=_schema(user_input or current),
            errors=errors,
        )
