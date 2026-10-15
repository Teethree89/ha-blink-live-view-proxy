"""Re-authentication has to change the token that setup actually uses.

Setup reads {**entry.data, **entry.options}, so options win. Configure saves
every field into options, which means that after one visit to Configure a
token written to `data` alone is never read again: the reauth form reports
success, the entry reloads on the old token, the proxy answers 401, and the
same prompt comes straight back. From the outside that is "re-entering the
populated token does not resolve the problem" (#74).

See conftest.py for how to run these.
"""

from __future__ import annotations

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
)

from .conftest import PROXY_TOKEN, stub_frontend_dependencies, without_frontend

STALE_TOKEN = "token-from-before-the-rotation"
# Nothing listens here, so a request to it is refused at once.
DEAD_URL = "http://127.0.0.1:9"


@pytest.fixture(autouse=True)
async def _frontend_stubbed(hass: HomeAssistant) -> None:
    await stub_frontend_dependencies(hass)


def _saved_through_configure(proxy: Any, token: str) -> dict[str, Any]:
    """What the options flow stores: every field, token included."""
    return {
        CONF_BASE_URL: proxy.base_url,
        CONF_TOKEN: token,
        CONF_STREAM_SECONDS: 30,
        CONF_BLINK_POLL_SECONDS: 300,
    }


def _entry(
    hass: HomeAssistant,
    *,
    data: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Blink Live View Proxy",
        data=data,
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


async def _reauthenticate(
    hass: HomeAssistant, entry: MockConfigEntry, token: str
) -> dict[str, Any]:
    """Run the reauth form to its end and let the reload it asks for finish."""
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    with without_frontend():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: token}
        )
        await hass.async_block_till_done()
    return result


def _token_in_force(entry: MockConfigEntry) -> str:
    """The token setup will use: the same merge __init__ performs."""
    return {**entry.data, **entry.options}.get(CONF_TOKEN, "")


async def test_reauth_replaces_a_token_saved_through_configure(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """The reported loop: reauth succeeds, and the stale token is still in force."""
    entry = _entry(
        hass,
        data={CONF_BASE_URL: fake_proxy.base_url, CONF_TOKEN: STALE_TOKEN},
        options=_saved_through_configure(fake_proxy, STALE_TOKEN),
    )

    result = await _reauthenticate(hass, entry, PROXY_TOKEN)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert _token_in_force(entry) == PROXY_TOKEN, (
        "the new token went somewhere setup does not look"
    )
    assert entry.state is config_entries.ConfigEntryState.LOADED, (
        "the entry reloaded on the old token and was refused again"
    )


async def test_reauth_keeps_other_options_when_it_replaces_the_token(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """Replacing the token must not reset the rest of what Configure saved."""
    options = _saved_through_configure(fake_proxy, STALE_TOKEN)
    options[CONF_STREAM_SECONDS] = 45
    options[CONF_BLINK_POLL_SECONDS] = 600
    entry = _entry(
        hass,
        data={CONF_BASE_URL: fake_proxy.base_url, CONF_TOKEN: STALE_TOKEN},
        options=options,
    )

    await _reauthenticate(hass, entry, PROXY_TOKEN)

    assert entry.options[CONF_STREAM_SECONDS] == 45
    assert entry.options[CONF_BLINK_POLL_SECONDS] == 600
    assert entry.options[CONF_BASE_URL] == fake_proxy.base_url


async def test_reauth_without_configure_still_updates_data(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """The ordinary case, and it must not start growing options it never had."""
    entry = _entry(
        hass,
        data={CONF_BASE_URL: fake_proxy.base_url, CONF_TOKEN: STALE_TOKEN},
    )

    result = await _reauthenticate(hass, entry, PROXY_TOKEN)

    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == PROXY_TOKEN
    assert CONF_TOKEN not in entry.options
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_reauth_checks_the_address_setup_uses(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """A URL changed in Configure is the one the token has to be good for.

    `data` still carries the address from first setup. Validating against it
    asks a proxy that is no longer the one in use, and a correct token is
    refused with "could not connect".
    """
    entry = _entry(
        hass,
        data={CONF_BASE_URL: DEAD_URL, CONF_TOKEN: STALE_TOKEN},
        options=_saved_through_configure(fake_proxy, STALE_TOKEN),
    )

    result = await _reauthenticate(hass, entry, PROXY_TOKEN)

    assert result["type"] is FlowResultType.ABORT, result.get("errors")
    assert result["reason"] == "reauth_successful"
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_reauth_with_a_wrong_token_stays_on_the_form(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """A rejected token is reported and nothing is written."""
    entry = _entry(
        hass,
        data={CONF_BASE_URL: fake_proxy.base_url, CONF_TOKEN: STALE_TOKEN},
        options=_saved_through_configure(fake_proxy, STALE_TOKEN),
    )
    result = await entry.start_reauth_flow(hass)

    with without_frontend():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: "still-not-the-token"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_TOKEN] == STALE_TOKEN
    assert entry.options[CONF_TOKEN] == STALE_TOKEN
