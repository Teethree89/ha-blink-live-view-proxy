"""The Blink entities built from the proxy's session, inside real Home Assistant.

Every camera gets a snapshot, a motion switch, motion and battery sensors,
temperature and the rest, the sync module gets an alarm panel, and each
control goes all the way through to blinkpy and back - with Home Assistant's
own Blink integration nowhere in sight.

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
    CONF_BLINK_POLL_SECONDS,
    CONF_TOKEN,
    DOMAIN,
)

from .conftest import PROXY_TOKEN

# What every install has had since 0.8: a live camera per Blink camera and the
# proxy's health sensor. Compared by unique id, because an install from before
# their ids were set outright may hold a different shape: Home Assistant
# derived them, and from 2026.9 it derives them with the device name in front.
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
    "binary_sensor.blink_proxy_114_cooper_connection",
    "sensor.blink_liveview_proxy_last_blink_refresh",
}


def _entry(hass: HomeAssistant, proxy: Any, **options: Any) -> MockConfigEntry:
    """An entry added to Home Assistant but not yet set up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Blink Live View Proxy",
        data={CONF_BASE_URL: proxy.base_url, CONF_TOKEN: PROXY_TOKEN},
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(
    hass: HomeAssistant,
    proxy: Any,
    entry: MockConfigEntry | None = None,
    **options: Any,
) -> MockConfigEntry:
    if entry is None:
        entry = _entry(hass, proxy, **options)
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


async def test_creates_every_entity(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    entry = await _setup(
        hass, fake_proxy, **{CONF_BLINK_POLL_SECONDS: 300}
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
    # The ids every dashboard, example and generator spells. Home Assistant
    # derived these two until they were set outright, and from 2026.9 it derives
    # them with the device name in front, so a fresh install did not get them.
    assert _live_entity_id(hass, entry, "SERIAL-A") == "camera.blink_live_driveway"
    assert _live_entity_id(hass, entry, "SERIAL-B") == "camera.blink_live_back_door"
    assert (
        er.async_get(hass).async_get_entity_id(
            "binary_sensor", DOMAIN, f"{entry.entry_id}_health"
        )
        == "binary_sensor.blink_liveview_proxy"
    )
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


async def test_registered_ids_are_left_alone(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """An install from before the ids were set outright keeps the ones it has.

    Somebody on 2026.9 who set up 1.0.0 holds the prefixed shape, and every
    automation and card of theirs names it. Setting the id must not move it.
    """
    entry = _entry(hass, fake_proxy)
    registry = er.async_get(hass)
    live = registry.async_get_or_create(
        "camera",
        DOMAIN,
        f"{entry.entry_id}_SERIAL-A_live",
        config_entry=entry,
        suggested_object_id="blink_driveway_blink_live_driveway",
    )
    health = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{entry.entry_id}_health",
        config_entry=entry,
        suggested_object_id="blink_live_view_proxy_blink_liveview_proxy",
    )
    await _setup(hass, fake_proxy, entry)

    assert _live_entity_id(hass, entry, "SERIAL-A") == live.entity_id
    assert live.entity_id == "camera.blink_driveway_blink_live_driveway"
    assert hass.states.get(live.entity_id) is not None, "and it is the one that is live"
    assert hass.states.get(health.entity_id) is not None
    assert hass.states.get("camera.blink_live_driveway") is None, "no second entity appears"
    assert hass.states.get("binary_sensor.blink_liveview_proxy") is None
    # The camera that was not registered yet gets the documented id.
    assert _live_entity_id(hass, entry, "SERIAL-B") == "camera.blink_live_back_door"


async def test_motion_switch_reaches_blinkpy(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    await _setup(hass, fake_proxy)
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


async def test_busy_camera_is_explained(hass: HomeAssistant, fake_proxy: Any) -> None:
    import pytest
    from homeassistant.exceptions import HomeAssistantError

    await _setup(hass, fake_proxy)
    fake_proxy.blink.cameras["Driveway"].busy = True
    with pytest.raises(HomeAssistantError, match="busy"):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": "switch.blink_proxy_driveway_motion_detection"},
            blocking=True,
        )
    assert hass.states.get("switch.blink_proxy_driveway_motion_detection").state == "on"


async def test_alarm_panel_reaches_blinkpy(hass: HomeAssistant, fake_proxy: Any) -> None:
    await _setup(hass, fake_proxy)
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

    await _setup(hass, fake_proxy)
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


async def test_snapshot_camera_says_when_blink_took_the_picture(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    """The age of the picture on offer, which snapshot_id alone cannot give.

    snapshot_id says that the picture changed. Only the time inside Blink's own
    address says when it was taken, and without it anything built on the
    entity can only measure when Home Assistant noticed (#75).
    """
    await _setup(hass, fake_proxy)
    snapshot = "camera.blink_proxy_driveway"
    assert hass.states.get(snapshot).attributes["snapshot_taken"] == (
        "1970-01-01T00:00:01+00:00"
    )

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.blink_proxy_driveway_refresh_snapshot"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(snapshot).attributes["snapshot_taken"] == (
        "1970-01-01T00:00:02+00:00"
    ), "a new picture brings the time it was taken"


async def test_live_view_loading_frame_uses_own_snapshot(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    import base64

    from homeassistant.components.camera import async_get_image

    await _setup(hass, fake_proxy)
    # camera.driveway (the official integration's) does not exist here.
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    frame = await async_get_image(hass, _live_entity_id(hass, entry, "SERIAL-A"))
    assert base64.b64encode(b"JPEG-1") in frame.content, (
        "the frame behind 'Starting live view' is the proxy's own snapshot"
    )


async def test_snapshot_refresh_view_without_official_integration(
    hass: HomeAssistant, fake_proxy: Any, hass_client: Any
) -> None:
    await _setup(hass, fake_proxy)
    assert not hass.services.has_service("blink", "trigger_camera")
    client = await hass_client()
    resp = await client.post("/api/blink_liveview_proxy/cameras/driveway/snapshot-refresh")
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["entity_id"] == "camera.blink_proxy_driveway"
    assert body["snapshot_url"].startswith("/api/camera_proxy/camera.blink_proxy_driveway")
    assert fake_proxy.blink.cameras["Driveway"].snaps == 1


async def test_failed_session_marks_entities_unavailable(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    entry = await _setup(hass, fake_proxy)
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
    entry = await _setup(hass, fake_proxy)
    unique_ids = {item.unique_id for item in _entries(hass, entry)}
    assert _baseline_unique_ids(entry) <= unique_ids, "live view still sets up"
    assert not any(uid.endswith("_motion_detection") for uid in unique_ids)


async def _not_found(_request: Any) -> Any:
    from aiohttp import web

    raise web.HTTPNotFound()


async def test_unload(hass: HomeAssistant, fake_proxy: Any) -> None:
    entry = await _setup(hass, fake_proxy)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("switch.blink_proxy_driveway_motion_detection").state == "unavailable"


async def test_panel_and_dashboard_use_own_entities(
    hass: HomeAssistant, fake_proxy: Any
) -> None:
    from custom_components.blink_liveview_proxy.dashboard_yaml import _camera_card
    from custom_components.blink_liveview_proxy.views import _panel_cameras

    entry = await _setup(hass, fake_proxy)
    runtime = hass.data[DOMAIN][entry.entry_id]
    cameras = {item["slug"]: item for item in _panel_cameras(hass, entry.entry_id, runtime)}
    driveway = cameras["driveway"]
    assert driveway["entity_id"] == "camera.blink_proxy_driveway"
    assert "motion_detection" in driveway["capabilities"]
    listed = {item["entity_id"] for item in driveway["entities"]}
    assert "switch.blink_proxy_driveway_motion_detection" in listed
    assert driveway["live_entity_id"] in listed, "one device holds the live camera too"

    card = _camera_card(driveway)
    assert "camera.blink_proxy_driveway" in card
    assert "switch.blink_proxy_driveway_motion_detection" in card, (
        "the motion button toggles the switch, not the motion binary sensor"
    )


async def test_generated_pill_names_the_sensor_the_install_holds(
    hass: HomeAssistant, fake_proxy: Any, hass_client: Any, hass_admin_user: Any
) -> None:
    """The panel's YAML points the Proxy pill at the health sensor that exists.

    A fresh install holds binary_sensor.blink_liveview_proxy. One set up on
    2026.9 or later before the id was set outright holds it with the device's
    name in front, and the registry keeps that, so the pill cannot assume.
    """
    entry = _entry(hass, fake_proxy)
    er.async_get(hass).async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{entry.entry_id}_health",
        config_entry=entry,
        suggested_object_id="blink_live_view_proxy_blink_liveview_proxy",
    )
    await _setup(hass, fake_proxy, entry)
    client = await hass_client()
    resp = await client.get("/api/blink_liveview_proxy/panel/yaml?format=view")
    assert resp.status == 200, await resp.text()
    text = (await resp.json())["yaml"]
    assert "entity: binary_sensor.blink_live_view_proxy_blink_liveview_proxy" in text
    assert "entity: binary_sensor.blink_liveview_proxy\n" not in text
