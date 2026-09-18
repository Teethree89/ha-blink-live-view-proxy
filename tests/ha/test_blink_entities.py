"""The Blink entities option, inside real Home Assistant.

Two promises are pinned here. With the option off, the entry creates exactly
the entities it always has and never asks the proxy for device state. With it
on, every camera gets a snapshot, a motion switch, motion and battery sensors,
temperature and the rest, the sync module gets an alarm panel, and each
control goes all the way through to blinkpy and back.

See conftest.py for how to run these.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blink_liveview_proxy.const import (
    CONF_BASE_URL,
    CONF_BLINK_ENTITIES,
    CONF_BLINK_POLL_SECONDS,
    CONF_TOKEN,
    DOMAIN,
)

from .conftest import PROXY_TOKEN

# What every install has had since 0.8: a live camera per Blink camera and the
# proxy's health sensor. Compared by unique id, because the entity ids Home
# Assistant derives for these two changed shape in 2026.9 (it prefixes the
# device name now) and that is not this option's doing.
BASELINE_SUFFIXES = {"_health", "_SERIAL-A_live", "_SERIAL-B_live"}

# The new entities set their ids outright, so these hold on every release.
PER_CAMERA = (
    "camera.blink_proxy_{slug}",
    "button.blink_proxy_{slug}_refresh_snapshot",
    "switch.blink_proxy_{slug}_motion_detection",
    "binary_sensor.blink_proxy_{slug}_motion",
    "binary_sensor.blink_proxy_{slug}_battery",
    "sensor.blink_proxy_{slug}_temperature",
    "sensor.blink_proxy_{slug}_wifi_signal",
    "sensor.blink_proxy_{slug}_battery_voltage",
)

NEW_ENTITIES = {
    template.format(slug=slug)
    for slug in ("driveway", "back_door")
    for template in PER_CAMERA
} | {
    "alarm_control_panel.blink_proxy_114_cooper",
    "sensor.blink_liveview_proxy_last_blink_refresh",
}


async def _setup(hass: HomeAssistant, proxy: Any, **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Blink Live View Proxy",
        data={CONF_BASE_URL: proxy.base_url, CONF_TOKEN: PROXY_TOKEN},
        options=options,
    )
    entry.add_to_hass(hass)
    # The Lovelace resource and the sidebar panel have nothing to do with
    # entities, and they need the whole frontend package installed. Both are
    # patched out, and their two dependencies marked as already loaded.
    hass.config.components.update({"frontend", "panel_custom"})
    # Always loaded in a real install; the snapshot view calls its
    # homeassistant.update_entity action.
    assert await async_setup_component(hass, "homeassistant", {})
    with (
        patch(
            "custom_components.blink_liveview_proxy._async_register_auth_panel",
            return_value=None,
        ),
        patch(
            "custom_components.blink_liveview_proxy._async_register_frontend_resource",
            return_value=None,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _entries(hass: HomeAssistant, entry: MockConfigEntry) -> list[er.RegistryEntry]:
    return er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


def _baseline_unique_ids(entry: MockConfigEntry) -> set[str]:
    return {f"{entry.entry_id}{suffix}" for suffix in BASELINE_SUFFIXES}


def _live_entity_id(hass: HomeAssistant, entry: MockConfigEntry, serial: str) -> str:
    return er.async_get(hass).async_get_entity_id(
        "camera", DOMAIN, f"{entry.entry_id}_{serial}_live"
    )


async def test_option_off_changes_nothing(hass: HomeAssistant, fake_proxy: Any) -> None:
    entry = await _setup(hass, fake_proxy)
    assert {item.unique_id for item in _entries(hass, entry)} == _baseline_unique_ids(entry)
    assert fake_proxy.asked("/devices") == 0, "no device state is asked for"
    assert fake_proxy.blink.refreshes == [], "and Blink is never polled"
    assert hass.states.get(_live_entity_id(hass, entry, "SERIAL-A")).state != "unavailable"

    explicitly_off = await _setup(hass, fake_proxy, **{CONF_BLINK_ENTITIES: False})
    assert {item.unique_id for item in _entries(hass, explicitly_off)} == (
        _baseline_unique_ids(explicitly_off)
    )
    assert fake_proxy.asked("/devices") == 0


async def test_option_on_creates_every_entity(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    entry = await _setup(
        hass, fake_proxy, **{CONF_BLINK_ENTITIES: True, CONF_BLINK_POLL_SECONDS: 300}
    )
    baseline = _baseline_unique_ids(entry)
    entries = _entries(hass, entry)
    assert baseline <= {item.unique_id for item in entries}, "nothing that was there goes"
    assert {
        item.entity_id for item in entries if item.unique_id not in baseline
    } == NEW_ENTITIES

    state = hass.states.get
    assert state("switch.blink_proxy_driveway_motion_detection").state == "on"
    assert state("binary_sensor.blink_proxy_driveway_motion").state == "off"
    assert state("binary_sensor.blink_proxy_driveway_battery").state == "off"
    # Blink reports Fahrenheit; the test instance is metric, so Home
    # Assistant shows 68 °F as 20 °C.
    temperature = state("sensor.blink_proxy_driveway_temperature")
    assert temperature.state == "20.0"
    assert temperature.attributes["unit_of_measurement"] == "°C"
    assert state("sensor.blink_proxy_driveway_wifi_signal").state == "-58"
    assert state("sensor.blink_proxy_driveway_battery_voltage").state == "1.65"
    assert state("alarm_control_panel.blink_proxy_114_cooper").state == "armed_away"
    assert state("camera.blink_proxy_driveway").attributes["snapshot_of"] == "driveway"
    assert "proxy_slug" not in state("camera.blink_proxy_driveway").attributes, (
        "the live camera is the only one the dialog may find by proxy_slug"
    )
    assert state("sensor.blink_liveview_proxy_last_blink_refresh").attributes["interval"] == 300
    assert fake_proxy.asked("/devices") >= 1

    # Same device as the live camera, so a camera stays one device.
    registry = er.async_get(hass)
    assert (
        registry.async_get("switch.blink_proxy_driveway_motion_detection").device_id
        == registry.async_get(_live_entity_id(hass, entry, "SERIAL-A")).device_id
    )


async def test_motion_switch_reaches_blinkpy(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    await _setup(hass, fake_proxy, **{CONF_BLINK_ENTITIES: True})
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.blink_proxy_driveway_motion_detection"},
        blocking=True,
    )
    camera = fake_proxy.blink.cameras["Driveway"]
    assert camera.arm_calls == [False]
    assert hass.states.get("switch.blink_proxy_driveway_motion_detection").state == "off"

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.blink_proxy_driveway_motion_detection"},
        blocking=True,
    )
    assert camera.arm_calls == [False, True]
    assert hass.states.get("switch.blink_proxy_driveway_motion_detection").state == "on"


async def test_alarm_panel_reaches_blinkpy(hass: HomeAssistant, fake_proxy: Any) -> None:
    await _setup(hass, fake_proxy, **{CONF_BLINK_ENTITIES: True})
    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_disarm",
        {"entity_id": "alarm_control_panel.blink_proxy_114_cooper"},
        blocking=True,
    )
    assert fake_proxy.blink.sync["114 Cooper"].arm_calls == [False]
    assert hass.states.get("alarm_control_panel.blink_proxy_114_cooper").state == "disarmed"

    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_arm_away",
        {"entity_id": "alarm_control_panel.blink_proxy_114_cooper"},
        blocking=True,
    )
    assert hass.states.get("alarm_control_panel.blink_proxy_114_cooper").state == "armed_away"


async def test_snapshot_button_and_camera_image(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    from homeassistant.components.camera import async_get_image

    await _setup(hass, fake_proxy, **{CONF_BLINK_ENTITIES: True})
    image = await async_get_image(hass, "camera.blink_proxy_driveway")
    assert image.content == b"JPEG-1"
    assert image.content_type == "image/jpeg"

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.blink_proxy_driveway_refresh_snapshot"},
        blocking=True,
    )
    assert fake_proxy.blink.cameras["Driveway"].snaps == 1
    image = await async_get_image(hass, "camera.blink_proxy_driveway")
    assert image.content == b"JPEG-2", "a new picture replaces the cached one"


async def test_live_view_loading_frame_uses_own_snapshot(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    import base64

    from homeassistant.components.camera import async_get_image

    await _setup(hass, fake_proxy, **{CONF_BLINK_ENTITIES: True})
    # camera.driveway (the official integration's) does not exist here.
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    frame = await async_get_image(hass, _live_entity_id(hass, entry, "SERIAL-A"))
    assert base64.b64encode(b"JPEG-1") in frame.content, (
        "the frame behind 'Starting live view' is the proxy's own snapshot"
    )


async def test_snapshot_refresh_view_without_official_integration(
    hass: HomeAssistant, fake_proxy: Any, hass_client: Any
) -> None:
    await _setup(hass, fake_proxy, **{CONF_BLINK_ENTITIES: True})
    assert not hass.services.has_service("blink", "trigger_camera")
    client = await hass_client()
    resp = await client.post("/api/blink_liveview_proxy/cameras/driveway/snapshot-refresh")
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["entity_id"] == "camera.blink_proxy_driveway"
    assert body["snapshot_url"].startswith("/api/camera_proxy/camera.blink_proxy_driveway")
    assert fake_proxy.blink.cameras["Driveway"].snaps == 1


async def test_snapshot_refresh_view_option_off_still_needs_official(
    hass: HomeAssistant, fake_proxy: Any, hass_client: Any
) -> None:
    await _setup(hass, fake_proxy)
    client = await hass_client()
    resp = await client.post("/api/blink_liveview_proxy/cameras/driveway/snapshot-refresh")
    assert resp.status == 404, "unchanged: without the option this needs blink.trigger_camera"
    assert "official Blink integration" in await resp.text()
    assert fake_proxy.blink.cameras["Driveway"].snaps == 0


async def test_failed_session_marks_entities_unavailable(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    entry = await _setup(hass, fake_proxy, **{CONF_BLINK_ENTITIES: True})
    poller = fake_proxy.app["device_poller"]
    poller.state = "auth_failed"
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("switch.blink_proxy_driveway_motion_detection").state == "unavailable"
    assert hass.states.get("alarm_control_panel.blink_proxy_114_cooper").state == "unavailable"
    # Live view does not depend on it.
    live = _live_entity_id(hass, entry, "SERIAL-A")
    assert hass.states.get(live).state != "unavailable"


async def test_old_proxy_without_devices_route(hass: HomeAssistant, fake_proxy: Any) -> None:
    for route in list(fake_proxy.app.router.routes()):
        if route.resource is not None and route.resource.canonical == "/devices":
            route._handler = _not_found  # type: ignore[attr-defined]
    entry = await _setup(hass, fake_proxy, **{CONF_BLINK_ENTITIES: True})
    unique_ids = {item.unique_id for item in _entries(hass, entry)}
    assert _baseline_unique_ids(entry) <= unique_ids, "live view still sets up"
    assert not any(uid.endswith("_motion_detection") for uid in unique_ids)


async def _not_found(_request: Any) -> Any:
    from aiohttp import web

    raise web.HTTPNotFound()


async def test_unload_with_option_on(hass: HomeAssistant, fake_proxy: Any) -> None:
    entry = await _setup(hass, fake_proxy, **{CONF_BLINK_ENTITIES: True})
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("switch.blink_proxy_driveway_motion_detection").state == "unavailable"
