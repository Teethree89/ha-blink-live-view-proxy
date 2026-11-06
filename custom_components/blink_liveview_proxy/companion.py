"""Install the companion add-on, once the user has said yes.

This is the only code in the integration that changes anything on the
Supervisor, so it is small, it is called from exactly one place (the config
flow's confirmation step), and it does the least that gets the add-on onto the
machine:

* it adds this project's repository to the add-on store, if it is not there;
* it installs the add-on, if it is not installed;
* it does not start it, configure it, or touch Blink credentials. The add-on
  needs the user's Blink login and, later, a 2FA PIN, and neither belongs in a
  config flow;
* it never replaces, updates or restarts an add-on that is already installed.

Supervisor is imported lazily and every failure is reported as a
CompanionInstallError. Home Assistant Container and Core have no Supervisor, and
the oldest release this integration supports predates the client library, so
none of that may be a reason the config flow fails to load.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .supervisor import ADDON_REPOSITORY_URL, is_addon_repository, store_addon_slug

LOGGER = logging.getLogger(__name__)

# Why an install did not happen, as the config flow's translation keys.
REASON_UNSUPPORTED = "unsupported"
REASON_FAILED = "failed"


class CompanionInstallError(Exception):
    """The add-on could not be installed; `reason` says which kind of no."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


async def async_install_companion(hass: HomeAssistant) -> str:
    """Add the repository and install the add-on; return the add-on's slug.

    Returns without changing anything when the add-on is already installed.
    """
    try:
        from aiohasupervisor import SupervisorError
        from aiohasupervisor.models import StoreAddRepository
        from homeassistant.components.hassio import get_supervisor_client
    except ImportError as err:
        raise CompanionInstallError(
            REASON_UNSUPPORTED, "This Home Assistant has no Supervisor client"
        ) from err

    try:
        store = get_supervisor_client(hass).store

        repository = _find_repository(await store.repositories_list())
        if repository is None:
            LOGGER.info("Adding %s to the add-on store", ADDON_REPOSITORY_URL)
            await store.add_repository(
                StoreAddRepository(repository=ADDON_REPOSITORY_URL)
            )
            repository = _find_repository(await store.repositories_list())
        if repository is None:
            raise CompanionInstallError(
                REASON_FAILED, "The repository is not listed after adding it"
            )

        slug = store_addon_slug(repository.slug, DOMAIN)
        if (await store.addon_info(slug)).installed:
            LOGGER.info("Companion add-on %s is already installed", slug)
            return slug

        # Supervisor refuses an add-on this machine cannot run (an unsupported
        # architecture, say) with a reason; ask first so that reason is the one
        # in the log rather than a half-started install.
        await store.addon_availability(slug)
        LOGGER.info("Installing companion add-on %s", slug)
        await store.install_addon(slug)
        return slug
    except SupervisorError as err:
        LOGGER.error("Supervisor could not install the companion add-on: %s", err)
        raise CompanionInstallError(REASON_FAILED, str(err)) from err


def _find_repository(repositories):
    """Return the store entry for this project's repository, if it is listed."""
    for repository in repositories:
        if is_addon_repository(repository.source):
            return repository
    return None
