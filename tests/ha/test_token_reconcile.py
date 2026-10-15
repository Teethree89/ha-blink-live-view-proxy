"""The add-on's token handoff, reconciled inside real Home Assistant (#74).

The add-on rewrites blink_liveview_proxy.token and .url in the config directory
on every start. A config entry keeps whatever token it was given, so a token
that changed since (an option edited, a restored backup, a fresh /data) left
the integration refused until someone typed the new one in.

These run the real setup and the real Configure form against the real proxy
routes. The rules under test: only the add-on's own address is ever trusted,
a handoff is adopted only after the proxy accepts it, and an unchanged one
costs no request.

See conftest.py for how to run these.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blink_liveview_proxy.const import (
    CONF_BASE_URL,
    CONF_BLINK_POLL_SECONDS,
    CONF_STREAM_SECONDS,
    CONF_TOKEN,
    DOMAIN,
    TOKEN_HANDOFF_FILE,
    URL_HANDOFF_FILE,
)

from .conftest import PROXY_TOKEN, stub_frontend_dependencies, without_frontend

STALE_TOKEN = "token-from-before-the-rotation"
ANOTHER_ADDRESS = "http://addon-somewhere-else:8088"


@pytest.fixture(autouse=True)
async def _frontend_stubbed(hass: HomeAssistant) -> None:
    await stub_frontend_dependencies(hass)


@pytest.fixture(autouse=True)
def _own_config_directory(hass: HomeAssistant, tmp_path: pathlib.Path) -> None:
    """Each test gets its own config directory, so handoff files never leak."""
    hass.config.config_dir = str(tmp_path)


def _share(hass: HomeAssistant, *, url: str | None, token: str | None) -> None:
    """What the add-on leaves behind at start, in either half."""
    if url is not None:
        pathlib.Path(hass.config.path(URL_HANDOFF_FILE)).write_text(f"{url}\n")
    if token is not None:
        pathlib.Path(hass.config.path(TOKEN_HANDOFF_FILE)).write_text(f"{token}\n")


def _entry(
    hass: HomeAssistant,
    proxy: Any,
    *,
    token: str,
    saved_through_configure: bool = False,
) -> MockConfigEntry:
    options: dict[str, Any] = {}
    if saved_through_configure:
        options = {
            CONF_BASE_URL: proxy.base_url,
            CONF_TOKEN: token,
            CONF_STREAM_SECONDS: 30,
            CONF_BLINK_POLL_SECONDS: 300,
        }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Blink Live View Proxy",
        data={CONF_BASE_URL: proxy.base_url, CONF_TOKEN: token},
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


async def _set_up(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    with without_frontend():
        loaded = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return loaded


def _token_in_force(entry: MockConfigEntry) -> str:
    return {**entry.data, **entry.options}.get(CONF_TOKEN, "")


def _default(result: dict[str, Any], key: str) -> Any:
    """The value a form field is pre-filled with."""
    for marker in result["data_schema"].schema:
        if marker.schema == key:
            return marker.default()
    raise AssertionError(f"{key} is not on the form")


# -- setup ---------------------------------------------------------------


async def test_setup_adopts_a_token_the_add_on_has_since_changed(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """The recovery this change exists for."""
    entry = _entry(hass, fake_proxy, token=STALE_TOKEN)
    _share(hass, url=fake_proxy.base_url, token=PROXY_TOKEN)

    assert await _set_up(hass, entry)

    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert entry.data[CONF_TOKEN] == PROXY_TOKEN
    assert CONF_TOKEN not in entry.options, "a token that lived in data stays there"
    assert fake_proxy.asked("/auth/status") == 1


async def test_setup_adopts_into_options_when_configure_holds_the_token(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """Options win over data, so that is where the new token has to go."""
    entry = _entry(hass, fake_proxy, token=STALE_TOKEN, saved_through_configure=True)
    _share(hass, url=fake_proxy.base_url, token=PROXY_TOKEN)

    assert await _set_up(hass, entry)

    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert _token_in_force(entry) == PROXY_TOKEN
    assert entry.options[CONF_STREAM_SECONDS] == 30, "other options are kept"


async def test_setup_leaves_a_proxy_at_another_address_alone(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """A handoff is the add-on's. It never speaks for a proxy somewhere else."""
    entry = _entry(hass, fake_proxy, token=STALE_TOKEN)
    _share(hass, url=ANOTHER_ADDRESS, token=PROXY_TOKEN)

    assert not await _set_up(hass, entry)

    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    assert _token_in_force(entry) == STALE_TOKEN
    assert fake_proxy.asked("/auth/status") == 0, "an unrelated proxy is not probed"


async def test_setup_does_not_adopt_a_token_the_proxy_refuses(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """Same address, but the file and the running proxy disagree: keep what we have."""
    entry = _entry(hass, fake_proxy, token=STALE_TOKEN)
    _share(hass, url=fake_proxy.base_url, token="not-what-the-proxy-runs")

    assert not await _set_up(hass, entry)

    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    assert _token_in_force(entry) == STALE_TOKEN
    assert fake_proxy.asked("/auth/status") == 1


async def test_setup_with_an_unchanged_handoff_asks_nothing(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """The ordinary restart: same token as before, so no extra request."""
    entry = _entry(hass, fake_proxy, token=PROXY_TOKEN)
    _share(hass, url=fake_proxy.base_url, token=PROXY_TOKEN)

    assert await _set_up(hass, entry)

    assert fake_proxy.asked("/auth/status") == 0


async def test_setup_without_a_handoff_changes_nothing(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """A Docker or systemd proxy has no add-on to hand anything off."""
    entry = _entry(hass, fake_proxy, token=PROXY_TOKEN)

    assert await _set_up(hass, entry)

    assert _token_in_force(entry) == PROXY_TOKEN
    assert fake_proxy.asked("/auth/status") == 0


# -- Configure -----------------------------------------------------------


async def test_configure_offers_the_token_the_add_on_shared(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """Configure used to show the saved, obsolete token, which can only fail."""
    entry = _entry(hass, fake_proxy, token=STALE_TOKEN, saved_through_configure=True)
    _share(hass, url=fake_proxy.base_url, token=PROXY_TOKEN)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert _default(result, CONF_TOKEN) == PROXY_TOKEN

    # Saving the pre-filled form is the whole fix from the user's side.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_BASE_URL: fake_proxy.base_url,
            CONF_TOKEN: _default(result, CONF_TOKEN),
            CONF_STREAM_SECONDS: 30,
            CONF_BLINK_POLL_SECONDS: 300,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TOKEN] == PROXY_TOKEN


async def test_configure_does_not_offer_a_token_the_proxy_refuses(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    entry = _entry(hass, fake_proxy, token=STALE_TOKEN, saved_through_configure=True)
    _share(hass, url=fake_proxy.base_url, token="not-what-the-proxy-runs")

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert _default(result, CONF_TOKEN) == STALE_TOKEN


async def test_configure_does_not_offer_another_proxys_token(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    entry = _entry(hass, fake_proxy, token=STALE_TOKEN, saved_through_configure=True)
    _share(hass, url=ANOTHER_ADDRESS, token=PROXY_TOKEN)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert _default(result, CONF_TOKEN) == STALE_TOKEN
    assert fake_proxy.asked("/auth/status") == 0


async def test_configure_without_a_handoff_shows_the_saved_token(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    entry = _entry(hass, fake_proxy, token=STALE_TOKEN, saved_through_configure=True)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert _default(result, CONF_TOKEN) == STALE_TOKEN
    assert fake_proxy.asked("/auth/status") == 0
