"""Config flow for the Blink live-view proxy integration."""

from __future__ import annotations

import logging
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
    CONF_BASE_URL,
    CONF_STREAM_SECONDS,
    CONF_TOKEN,
    DEFAULT_BASE_URL,
    DEFAULT_STREAM_SECONDS,
    DOMAIN,
)

LOGGER = logging.getLogger(__name__)


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


async def _validate_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Validate the proxy URL and token."""
    client = BlinkLiveviewProxyClient(
        async_get_clientsession(hass), data[CONF_BASE_URL], data.get(CONF_TOKEN)
    )
    await client.async_get_health()
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
                        title="Blink Liveview Proxy",
                        data=data,
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input or {}),
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
