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

import asyncio
import logging

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .supervisor import ADDON_REPOSITORY_URL, is_addon_repository, store_addon_slug

LOGGER = logging.getLogger(__name__)

# Why an install did not happen, as the config flow's translation keys.
REASON_UNSUPPORTED = "unsupported"
REASON_FAILED = "failed"
REASON_PENDING = "still_installing"
INSTALL_TIMEOUT = 20 * 60
INSTALL_POLL_SECONDS = 2


async def _install_running(client, slug: str) -> bool:
    """Supervisor jobs survive an HTTP timeout and an abandoned config flow."""
    def running(jobs):
        return any(
            (job.reference == slug and not job.done)
            or running(job.child_jobs)
            for job in jobs
        )

    return running((await client.jobs.info()).jobs)


async def _wait_installed(client, slug: str) -> None:
    """Wait for the one existing job; never issue another install request."""
    missing = 0
    try:
        async with asyncio.timeout(INSTALL_TIMEOUT):
            while not (await client.store.addon_info(slug)).installed:
                missing = 0 if await _install_running(client, slug) else missing + 1
                # Allow time for a newly accepted job to become visible.
                if missing >= 5:
                    raise CompanionInstallError(REASON_FAILED)
                await asyncio.sleep(INSTALL_POLL_SECONDS)
    except TimeoutError as err:
        raise CompanionInstallError(REASON_PENDING) from err


class CompanionInstallError(Exception):
    """The add-on could not be installed; `reason` says which kind of no."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


async def async_install_companion(hass: HomeAssistant) -> str:
    """Add the repository and install the add-on; return the add-on's slug.

    Returns without changing anything when the add-on is already installed.
    """
    lock = hass.data.setdefault(DOMAIN, {}).setdefault("_companion_install_lock", asyncio.Lock())
    async with lock:
        return await _async_install_companion(hass)


async def _async_install_companion(hass: HomeAssistant) -> str:
    """Serialize consented flows as well as reconciling Supervisor jobs."""
    try:
        from aiohasupervisor import SupervisorError
        from aiohasupervisor.models import StoreAddonInstall, StoreAddRepository
        from homeassistant.components.hassio import get_supervisor_client
    except ImportError as err:
        raise CompanionInstallError(
            REASON_UNSUPPORTED, "This Home Assistant has no Supervisor client"
        ) from err

    try:
        client = get_supervisor_client(hass)
        store = client.store

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
        if not await _install_running(client, slug):
            await store.addon_availability(slug)
            LOGGER.info("Installing companion add-on %s", slug)
            try:
                await store.install_addon(slug, StoreAddonInstall(background=True))
            except (SupervisorError, TimeoutError):
                # Some Supervisors return unknown_error while the build keeps
                # running. Reconcile before allowing another installation.
                if not (await store.addon_info(slug)).installed and not await _install_running(client, slug):
                    raise
        await _wait_installed(client, slug)
        return slug
    except (SupervisorError, TimeoutError) as err:
        LOGGER.error("Supervisor could not confirm companion installation (%s)", type(err).__name__)
        raise CompanionInstallError(REASON_FAILED) from err


def _find_repository(repositories):
    """Return the store entry for this project's repository, if it is listed."""
    for repository in repositories:
        if is_addon_repository(repository.source):
            return repository
    return None
