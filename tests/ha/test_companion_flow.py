"""First-run setup offers to install the companion add-on, and only on consent.

The integration is half a product: the proxy that signs in to Blink is the other
half. On Home Assistant OS or Supervised the config flow can put that half in
place, which changes the user's system, so these pin the promises that go with
it:

* nothing is added or installed until the confirmation step is submitted;
* an add-on that is already installed is never reinstalled or replaced;
* a Supervisor failure ends in a screen that offers a retry or the manual
  route, never a dead end;
* Container, Core and existing add-on users see the connection form they
  always had, with no extra question in front of it.

Supervisor is faked at its client library, so no Supervisor is needed. See
conftest.py for how to run these.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohasupervisor import SupervisorError
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.blink_liveview_proxy.const import (
    DOMAIN,
    TOKEN_HANDOFF_FILE,
)
from custom_components.blink_liveview_proxy.supervisor import ADDON_REPOSITORY_URL

from .conftest import stub_frontend_dependencies

REPOSITORY_SLUG = "a1b2c3d4"
ADDON_SLUG = f"{REPOSITORY_SLUG}_{DOMAIN}"


@pytest.fixture(autouse=True)
async def _frontend_stubbed(hass: HomeAssistant) -> None:
    await stub_frontend_dependencies(hass)


@pytest.fixture(autouse=True)
def _private_config_dir(hass: HomeAssistant, tmp_path: Path) -> None:
    """Keep the handoff file out of the harness's config dir, which tests share."""
    hass.config.config_dir = str(tmp_path)


class FakeStore:
    """The slice of Supervisor's store API the installer uses."""

    def __init__(
        self,
        *,
        repository_listed: bool = False,
        addon_installed: bool = False,
        fail_on: str | None = None,
    ) -> None:
        self.repository_listed = repository_listed
        self.addon_installed = addon_installed
        self.fail_on = fail_on
        self.calls: list[str] = []

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_on == name:
            raise SupervisorError("simulated Supervisor failure")

    async def repositories_list(self) -> list[Any]:
        self._maybe_fail("repositories_list")
        listed = [SimpleNamespace(source="https://example.org/other", slug="zzzz")]
        if self.repository_listed:
            # Spelled the way Supervisor may have stored it.
            listed.append(
                SimpleNamespace(source=ADDON_REPOSITORY_URL + ".git", slug=REPOSITORY_SLUG)
            )
        return listed

    async def add_repository(self, _repository: Any) -> None:
        self._maybe_fail("add_repository")
        self.repository_listed = True

    async def addon_info(self, slug: str) -> Any:
        self._maybe_fail("addon_info")
        assert slug == ADDON_SLUG
        return SimpleNamespace(installed=self.addon_installed)

    async def addon_availability(self, slug: str) -> None:
        self._maybe_fail("addon_availability")

    async def install_addon(self, slug: str, options=None) -> None:
        self._maybe_fail("install_addon")
        assert slug == ADDON_SLUG
        assert options.background is True
        self.addon_installed = True


@pytest.fixture
def supervisor_install(hass: HomeAssistant):
    """Home Assistant OS, with no add-on and no handoff token yet."""
    hass.config.components.add("hassio")
    holder: dict[str, Any] = {"store": FakeStore(), "addons": {}}

    def client(_hass: HomeAssistant) -> MagicMock:
        from unittest.mock import AsyncMock

        return MagicMock(store=holder["store"], jobs=SimpleNamespace(info=AsyncMock(return_value=SimpleNamespace(jobs=[]))))

    with (
        patch("homeassistant.components.hassio.get_supervisor_client", client),
        patch(
            "homeassistant.components.hassio.get_addons_info",
            lambda _hass: holder["addons"],
        ),
    ):
        yield holder


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _choose(hass: HomeAssistant, result: dict[str, Any], option: str):
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )


async def _consent_and_wait(hass: HomeAssistant, result: dict[str, Any]):
    """Submit the confirmation and follow the progress screen to its end."""
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


async def test_container_user_goes_straight_to_the_connection_form(
    hass: HomeAssistant,
) -> None:
    """No Supervisor, no extra question: the form they always had."""
    result = await _start(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connect"


async def test_existing_addon_user_is_not_asked_to_install_again(
    hass: HomeAssistant, supervisor_install: dict[str, Any]
) -> None:
    """The add-on has handed over its token: the one-click path is untouched."""
    with open(hass.config.path(TOKEN_HANDOFF_FILE), "w", encoding="utf-8") as handle:
        handle.write("shared-token")

    result = await _start(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connect"
    assert supervisor_install["store"].calls == []


async def test_first_run_on_supervisor_offers_install_or_manual(
    hass: HomeAssistant, supervisor_install: dict[str, Any]
) -> None:
    result = await _start(hass)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["install_companion", "connect"]


async def test_choosing_manual_setup_shows_the_connection_form(
    hass: HomeAssistant, supervisor_install: dict[str, Any]
) -> None:
    result = await _choose(hass, await _start(hass), "connect")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connect"
    assert supervisor_install["store"].calls == []


async def test_nothing_changes_until_the_confirmation_is_submitted(
    hass: HomeAssistant, supervisor_install: dict[str, Any]
) -> None:
    """Opening the confirmation screen, or walking away from it, is free."""
    result = await _choose(hass, await _start(hass), "install_companion")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "install_companion"
    assert result["description_placeholders"] == {"repository": ADDON_REPOSITORY_URL}
    assert supervisor_install["store"].calls == []

    hass.config_entries.flow.async_abort(result["flow_id"])
    assert supervisor_install["store"].calls == []


async def test_consent_adds_the_repository_and_installs_the_addon(
    hass: HomeAssistant, supervisor_install: dict[str, Any]
) -> None:
    store: FakeStore = supervisor_install["store"]
    result = await _choose(hass, await _start(hass), "install_companion")

    result = await _consent_and_wait(hass, result)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "companion_ready"
    assert "add_repository" in store.calls
    assert "install_addon" in store.calls
    assert store.addon_installed


async def test_a_repository_already_added_is_not_added_twice(
    hass: HomeAssistant, supervisor_install: dict[str, Any]
) -> None:
    """Supervisor may list it with a .git suffix; that is still the same one."""
    store = supervisor_install["store"] = FakeStore(repository_listed=True)
    result = await _choose(hass, await _start(hass), "install_companion")

    result = await _consent_and_wait(hass, result)

    assert result["step_id"] == "companion_ready"
    assert "add_repository" not in store.calls
    assert "install_addon" in store.calls


async def test_an_installed_addon_is_never_reinstalled(
    hass: HomeAssistant, supervisor_install: dict[str, Any]
) -> None:
    """Even if it gets installed while the dialog is open."""
    store = supervisor_install["store"] = FakeStore(
        repository_listed=True, addon_installed=True
    )
    result = await _choose(hass, await _start(hass), "install_companion")

    result = await _consent_and_wait(hass, result)

    assert result["step_id"] == "companion_ready"
    assert "install_addon" not in store.calls
    assert "add_repository" not in store.calls


async def test_installed_but_not_started_skips_the_offer(
    hass: HomeAssistant, supervisor_install: dict[str, Any]
) -> None:
    """The add-on is there; what is missing is the user starting it."""
    supervisor_install["addons"] = {ADDON_SLUG: {"state": "stopped"}}

    result = await _start(hass)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "companion_ready"
    assert supervisor_install["store"].calls == []


async def test_after_the_guidance_the_user_continues_to_the_connection_form(
    hass: HomeAssistant, supervisor_install: dict[str, Any]
) -> None:
    supervisor_install["addons"] = {ADDON_SLUG: {"state": "stopped"}}
    result = await _choose(hass, await _start(hass), "connect")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connect"


@pytest.mark.parametrize("failing_call", ["add_repository", "addon_availability", "install_addon"])
async def test_a_supervisor_failure_offers_retry_or_manual_setup(
    hass: HomeAssistant, supervisor_install: dict[str, Any], failing_call: str
) -> None:
    store = supervisor_install["store"] = FakeStore(fail_on=failing_call)
    result = await _choose(hass, await _start(hass), "install_companion")

    result = await _consent_and_wait(hass, result)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "install_failed"
    assert result["menu_options"] == ["install_companion", "connect"]
    assert result["description_placeholders"] == {"reason": "failed"}
    assert not store.addon_installed

    # The retry works once Supervisor does.
    store.fail_on = None
    result = await _choose(hass, result, "install_companion")
    result = await _consent_and_wait(hass, result)
    assert result["step_id"] == "companion_ready"
    assert store.addon_installed


async def test_a_home_assistant_without_the_supervisor_client_says_so(
    hass: HomeAssistant, supervisor_install: dict[str, Any]
) -> None:
    """The oldest supported release predates the library; it must not crash."""
    result = await _choose(hass, await _start(hass), "install_companion")

    with patch.dict("sys.modules", {"aiohasupervisor": None}):
        result = await _consent_and_wait(hass, result)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "install_failed"
    assert result["description_placeholders"] == {"reason": "unsupported"}
