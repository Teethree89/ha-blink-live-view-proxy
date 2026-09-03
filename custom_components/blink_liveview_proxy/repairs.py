"""The Fix button behind the "your proxy needs updating" notices.

Home Assistant cannot reach onto another host on its own, but it does not have
to. A systemd install already carries an updater unit that install-proxy.sh put
there, and the proxy will start it when asked; when the proxy is the add-on,
Supervisor will do it instead. Both paths are start-and-confirm: the proxy
restarts as part of updating, so success shows up as a new version on the next
poll, never in the response to the request that began it.

The coordinator only marks an issue fixable when one of those two paths exists,
so a flow reaching here should have somewhere to go. It can still find the door
shut - an update already running, an add-on Supervisor cannot name - and each
of those aborts with its own sentence rather than a generic failure.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_BASE_URL
from .updates import (
    ABORT_ENTRY_GONE,
    UpdateAborted,
    async_start_update,
    runtime,
    status,
)
from .version_check import describe, infer_version


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Build the flow for whichever entry raised this issue."""
    entry_id = str((data or {}).get("entry_id") or "")
    if not entry_id:
        # Issues raised before the id was carried in data still name the entry
        # in their own id: "proxy_behind_<entry_id>".
        entry_id = issue_id.rsplit("_", 1)[-1]
    return ProxyUpdateRepairFlow(entry_id)


class ProxyUpdateRepairFlow(RepairsFlow):
    """Ask, then start the update the proxy or Supervisor already knows how to do."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        """Confirm, then start. Nothing is touched until the form comes back."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason=ABORT_ENTRY_GONE)

        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders=self._describe(entry),
            )

        try:
            await async_start_update(self.hass, self._entry_id)
        except UpdateAborted as err:
            return self.async_abort(reason=err.reason)
        return self.async_create_entry(title="", data={})

    def _runtime(self) -> dict:
        return runtime(self.hass, self._entry_id)

    def _status(self) -> dict | None:
        return status(self.hass, self._entry_id)

    def _describe(self, entry: ConfigEntry) -> dict[str, str]:
        status = self._status()
        return {
            "base_url": str(entry.data.get(CONF_BASE_URL, "the proxy")),
            "proxy_version": describe(infer_version(status)),
        }
