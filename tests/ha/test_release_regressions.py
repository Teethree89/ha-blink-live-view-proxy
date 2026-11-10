"""Release regressions through the real HA views and Supervisor client boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.blink_liveview_proxy import companion, views
from custom_components.blink_liveview_proxy.api import ProxyConnectionError
from custom_components.blink_liveview_proxy.failures import failure_payload
from custom_components.blink_liveview_proxy.prerequisites import build


@pytest.mark.parametrize("status,reason", [(400, "invalid_request"), (409, "auth_conflict")])
async def test_auth_action_preserves_client_errors(hass, hass_client, status, reason):
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "http", {})
    hass.http.register_view(views.BlinkLiveviewProxyAuthActionView(hass))
    proxy = SimpleNamespace(async_start_auth=AsyncMock(side_effect=ProxyConnectionError("private", status)))
    with patch.object(views, "_auth_client", return_value=proxy):
        client = await hass_client()
        response = await client.post("/api/blink_liveview_proxy/auth/login", json={})
    assert response.status == status
    body = await response.json()
    assert body["reason"] == reason
    assert "private" not in str(body)


@pytest.mark.parametrize("method,expected", [("supervisor", "Apps"), ("container", "container"), ("systemd", "systemctl")])
def test_installation_specific_recovery(method, expected):
    payload = failure_payload(None, method=method)
    assert expected in payload["remedy"]
    if method != "systemd":
        assert "systemctl" not in payload["remedy"]


@pytest.mark.parametrize("version,store", [("2026.9.0", "HACS"), ("2026.11.1", "Marketplace")])
def test_update_readout_uses_the_available_store(version, store):
    rows = build({"ha_version": version, "hacs_update": {"found": True, "installed": "1.3.0"}})
    row = next(row for row in rows if row["key"] == "integration_update")
    assert store in row["detail"]
    assert store in row["instructions"][0]


async def test_overview_uses_options_url(hass):
    runtime = {"coordinator": SimpleNamespace(data={})}
    entry = SimpleNamespace(title="Proxy", data={"base_url": "http://old"}, options={"base_url": "http://new"})
    with (
        patch.object(views, "_runtime_entry", return_value=("entry", runtime)),
        patch.object(hass.config_entries, "async_get_entry", return_value=entry),
        patch("homeassistant.loader.async_get_integration", AsyncMock(return_value=SimpleNamespace(version="1.3.0"))),
        patch.object(views, "_prerequisite_facts", AsyncMock(return_value={})),
        patch.object(views, "_panel_cameras", return_value=[]),
    ):
        result = await views._panel_payload(hass)
    assert result["base_url"] == "http://new"


async def test_install_error_reconciles_running_build_instead_of_installing_twice(hass):
    from aiohasupervisor import SupervisorError

    slug = "test_blink_liveview_proxy"
    store = SimpleNamespace(
        repositories_list=AsyncMock(return_value=[SimpleNamespace(source=companion.ADDON_REPOSITORY_URL, slug="test")]),
        addon_info=AsyncMock(side_effect=[SimpleNamespace(installed=False), SimpleNamespace(installed=False), SimpleNamespace(installed=True)]),
        addon_availability=AsyncMock(),
        install_addon=AsyncMock(side_effect=SupervisorError("timeout")),
    )
    active = SimpleNamespace(reference=slug, done=False, child_jobs=[])
    jobs = SimpleNamespace(info=AsyncMock(side_effect=[SimpleNamespace(jobs=[]), SimpleNamespace(jobs=[active])]))
    with patch("homeassistant.components.hassio.get_supervisor_client", return_value=SimpleNamespace(store=store, jobs=jobs)):
        assert await companion.async_install_companion(hass) == slug
    store.install_addon.assert_awaited_once()


async def test_retry_waits_for_existing_build(hass):
    slug = "test_blink_liveview_proxy"
    store = SimpleNamespace(
        repositories_list=AsyncMock(return_value=[SimpleNamespace(source=companion.ADDON_REPOSITORY_URL, slug="test")]),
        addon_info=AsyncMock(side_effect=[SimpleNamespace(installed=False), SimpleNamespace(installed=True)]),
        install_addon=AsyncMock(),
    )
    jobs = SimpleNamespace(info=AsyncMock(return_value=SimpleNamespace(jobs=[SimpleNamespace(reference=slug, done=False, child_jobs=[])])))
    with patch("homeassistant.components.hassio.get_supervisor_client", return_value=SimpleNamespace(store=store, jobs=jobs)):
        assert await companion.async_install_companion(hass) == slug
    store.install_addon.assert_not_called()
