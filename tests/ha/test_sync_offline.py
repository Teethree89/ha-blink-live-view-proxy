"""A sync module that has gone offline, seen from Home Assistant.

Blink keeps reporting the last arm state for a sync module that is no longer
reachable, so the alarm panel alone cannot say whether the system is really
watching. The proxy already knows: its device row carries the module's status.
These tests pin that the status reaches Home Assistant.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.blink_liveview_proxy.const import DOMAIN

from .test_blink_entities import _entry, _setup

PANEL = "alarm_control_panel.blink_proxy_114_cooper"
CONNECTIVITY = "binary_sensor.blink_proxy_114_cooper_connection"


def _offline(proxy: Any) -> None:
    proxy.blink.sync["114 Cooper"].status = "offline"


async def test_panel_alone_cannot_say_the_module_is_gone(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """The gap this change closes: armed reads the same either way."""
    _offline(fake_proxy)
    await _setup(hass, fake_proxy)
    panel = hass.states.get(PANEL)
    assert panel.state == "armed_away", (
        "Blink still reports the last arm state for an unreachable module"
    )
    assert panel.state != "unavailable", (
        "the panel stays usable, so arming is never blocked by a blip"
    )


async def test_status_reaches_home_assistant(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """Online is the normal case, and it is visible."""
    await _setup(hass, fake_proxy)
    assert hass.states.get(CONNECTIVITY).state == "on"
    assert hass.states.get(PANEL).attributes["status"] == "online"


async def test_offline_is_visible(hass: HomeAssistant, fake_proxy: Any) -> None:
    """The whole point: a module that is gone can be seen and automated on."""
    _offline(fake_proxy)
    await _setup(hass, fake_proxy)
    connection = hass.states.get(CONNECTIVITY)
    assert connection.state == "off"
    assert connection.attributes["device_class"] == "connectivity"
    assert hass.states.get(PANEL).attributes["status"] == "offline"


async def test_connectivity_is_diagnostic_and_on_the_sync_device(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """It belongs to the sync module's device, beside its alarm panel."""
    await _setup(hass, fake_proxy)
    registry = er.async_get(hass)
    connection = registry.async_get(CONNECTIVITY)
    assert connection.entity_category == EntityCategory.DIAGNOSTIC
    assert connection.device_id == registry.async_get(PANEL).device_id


async def test_upgrade_keeps_the_existing_alarm_panel(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """Adding an entity to the sync module must not move the panel's ids.

    An install that already has the panel keeps that entity, with whatever the
    owner renamed or hid, instead of gaining a second one beside it.
    """
    entry = _entry(hass, fake_proxy)
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(
        "alarm_control_panel",
        DOMAIN,
        f"{entry.entry_id}_sync_2001_arm",
        config_entry=entry,
        suggested_object_id="blink_proxy_114_cooper",
    )
    await _setup(hass, fake_proxy, entry)

    panels = [
        item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.domain == "alarm_control_panel"
    ]
    assert [item.entity_id for item in panels] == [existing.entity_id]
    assert panels[0].unique_id == f"{entry.entry_id}_sync_2001_arm"
    assert hass.states.get(PANEL).state == "armed_away"
