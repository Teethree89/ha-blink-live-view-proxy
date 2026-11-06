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
    CONF_BLINK_POLL_SECONDS,
    CONF_STREAM_SECONDS,
    CONF_TOKEN,
    DEFAULT_BASE_URL,
    DEFAULT_BLINK_POLL_SECONDS,
    DEFAULT_STREAM_SECONDS,
    DOMAIN,
    MAX_BLINK_POLL_SECONDS,
    MIN_BLINK_POLL_SECONDS,
    TOKEN_HANDOFF_FILE,
    URL_HANDOFF_FILE,
)
from .companion import CompanionInstallError, async_install_companion
from .supervisor import ADDON_REPOSITORY_URL, addon_internal_url, addon_slug

LOGGER = logging.getLogger(__name__)

# How long one candidate address gets to answer /health while the form is
# being built. The failures that matter here — no such host, connection
# refused — come back in milliseconds; this only bounds the case where
# something swallows the packets, and nobody should wait on the API's full
# ten seconds per candidate to be shown a form they can edit.
PROBE_TIMEOUT = 4


def _schema(defaults: dict[str, Any], *, blink_options: bool = False) -> vol.Schema:
    """Build the setup/options schema.

    The Blink refresh interval appears only in Options: the default suits
    almost everyone, and setup is about reaching the proxy.
    """
    blink_fields: dict[Any, Any] = {}
    if blink_options:
        blink_fields = {
            vol.Optional(
                CONF_BLINK_POLL_SECONDS,
                default=defaults.get(
                    CONF_BLINK_POLL_SECONDS, DEFAULT_BLINK_POLL_SECONDS
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_BLINK_POLL_SECONDS,
                    max=MAX_BLINK_POLL_SECONDS,
                    step=30,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
        }
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
            **blink_fields,
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



def token_update(
    entry: config_entries.ConfigEntry, token: str
) -> dict[str, dict[str, Any]]:
    """The `async_update_entry` argument that makes `token` the one in force.

    Setup runs on {**entry.data, **entry.options}, so options win. Configure
    saves every field into options, token included, and from then on a token
    written to `data` alone is never read again: the update reports success,
    the entry reloads on the old token, and the proxy refuses it again. Write
    the new token to wherever the current one lives.
    """
    if CONF_TOKEN in entry.options:
        return {"options": {**entry.options, CONF_TOKEN: token}}
    return {"data": {**entry.data, CONF_TOKEN: token}}


async def _async_verified_handoff_token(
    hass: HomeAssistant, base_url: str, current_token: str | None
) -> str:
    """Use a rotated add-on token only for its own URL and after validation.

    The handoff file is rewritten whenever the add-on starts, but existing
    config entries keep the token captured during setup. Never take a handoff
    token for a manually configured or unrelated proxy. Even when the URL
    matches, validate against the live /auth/status route before using it.
    """
    try:
        configured_url = normalize_base_url(base_url)
    except ProxyConnectionError:
        return ""
    shared_url = await _async_handoff_url(hass)
    if not shared_url or configured_url != shared_url:
        return ""
    shared_token = await _async_handoff_token(hass)
    if not shared_token or shared_token == (current_token or "").strip():
        return ""

    client = BlinkLiveviewProxyClient(
        async_get_clientsession(hass), configured_url, shared_token
    )
    try:
        await client.async_get_auth_status()
    except (ProxyAuthError, ProxyConnectionError):
        return ""
    return shared_token


def _installed_addon_slug(hass: HomeAssistant) -> str | None:
    """Return this add-on's slug if Supervisor lists it as installed."""
    try:
        from homeassistant.components.hassio import get_addons_info

        return addon_slug(get_addons_info(hass) or {}, DOMAIN)
    except Exception:  # noqa: BLE001 - no Supervisor here, or it moved this helper
        return None


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

    def __init__(self) -> None:
        """Hold the background install while its progress screen is showing."""
        self._install_task: asyncio.Task[str] | None = None
        self._install_error = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Route first-time setup: offer the add-on only where it can help.

        The menu appears on a Supervisor install that has neither the add-on's
        shared token nor the add-on itself. Everyone else — an existing add-on
        install, or a Container, Core or remote proxy — goes straight to the
        connection form they always had.
        """
        if "hassio" in self.hass.config.components:
            if not await _async_handoff_token(self.hass):
                if _installed_addon_slug(self.hass):
                    # Installed but never started: nothing to offer to install.
                    return await self.async_step_companion_ready()
                return self.async_show_menu(
                    step_id="user",
                    menu_options=["install_companion", "connect"],
                )
        return await self.async_step_connect()

    async def async_step_install_companion(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask first. Nothing is added or installed until this is submitted."""
        if user_input is None:
            return self.async_show_form(
                step_id="install_companion",
                data_schema=vol.Schema({}),
                description_placeholders={"repository": ADDON_REPOSITORY_URL},
            )
        return await self.async_step_installing()

    async def async_step_installing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Install in the background; pulling the add-on image can take minutes."""
        if self._install_task is None:
            self._install_task = self.hass.async_create_task(
                async_install_companion(self.hass)
            )
        task = self._install_task
        if not task.done():
            return self.async_show_progress(
                step_id="installing",
                progress_action="install_companion",
                progress_task=task,
            )

        self._install_task = None
        try:
            await task
        except CompanionInstallError as err:
            self._install_error = err.reason
            return self.async_show_progress_done(next_step_id="install_failed")
        except Exception:  # noqa: BLE001
            LOGGER.exception("Unexpected error installing the companion add-on")
            self._install_error = "failed"
            return self.async_show_progress_done(next_step_id="install_failed")
        return self.async_show_progress_done(next_step_id="companion_ready")

    async def async_step_install_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Say why, and offer a retry or the manual route; never dead-end."""
        return self.async_show_menu(
            step_id="install_failed",
            menu_options=["install_companion", "connect"],
            description_placeholders={"reason": self._install_error},
        )

    async def async_step_companion_ready(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Send the user to configure and start the add-on, then carry on.

        A menu rather than a form on purpose. When the install finishes at once
        (the add-on was already there) Home Assistant hands this step the
        confirmation's own empty submit, and a form with nothing in it cannot
        tell that from the user pressing Submit: the guidance would flash past.
        A menu only moves on for a real choice.
        """
        return self.async_show_menu(
            step_id="companion_ready", menu_options=["connect"]
        )

    async def async_step_connect(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Connect to a running proxy: the add-on, or one on Linux or Docker."""
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
            step_id="connect",
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

        # What setup runs on, not what first setup stored: a URL or token
        # changed in Configure lives in options, and that is the proxy this
        # token has to be good for.
        current = {**entry.data, **entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input.get(CONF_TOKEN, "").strip()
            try:
                await _validate_input(self.hass, {**current, CONF_TOKEN: token})
            except ProxyAuthError:
                errors["base"] = "invalid_auth"
            except ProxyConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected Blink live-view proxy reauth error")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    entry, **token_update(entry, token)
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        shared = await _async_handoff_token(self.hass)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_TOKEN,
                        default=shared or current.get(CONF_TOKEN, ""),
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
                    CONF_BLINK_POLL_SECONDS: int(
                        user_input.get(
                            CONF_BLINK_POLL_SECONDS, DEFAULT_BLINK_POLL_SECONDS
                        )
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

        defaults = user_input or current
        if user_input is None:
            # Configure previously displayed the saved (possibly obsolete)
            # token instead of the token the add-on currently shared. Check
            # that the new value really works before offering it in the form.
            shared = await _async_verified_handoff_token(
                self.hass, current[CONF_BASE_URL], current.get(CONF_TOKEN)
            )
            if shared:
                defaults = {**current, CONF_TOKEN: shared}

        return self.async_show_form(
            step_id="init",
            data_schema=_schema(defaults, blink_options=True),
            errors=errors,
        )
