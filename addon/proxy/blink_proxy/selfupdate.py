"""Whether this proxy can replace its own code, and starting it when it can.

The proxy gets installed three ways and only one of them can update itself. A
systemd host install ships an updater unit that reruns bootstrap.sh; the add-on
belongs to Supervisor; a container cannot rewrite the image it is running from.

So most of this module's work is saying *no* accurately. The integration puts a
Fix button in front of the user based on what it reads here, and a button that
cannot work is worse than no button at all.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any

from .constants import LOGGER_NAME

LOGGER = logging.getLogger(LOGGER_NAME)

# Both are put there by scripts/install-proxy.sh. The unit is a separate
# one-shot rather than something this process runs itself - see start().
UPDATE_UNIT = "blink-liveview-proxy-update.service"
UPDATE_SCRIPT = Path("/usr/local/sbin/blink-liveview-proxy-update.sh")
UPDATE_UNIT_FILE = Path("/etc/systemd/system") / UPDATE_UNIT
# Docker writes this into every container it starts, and nothing else does.
DOCKER_MARKER = Path("/.dockerenv")

METHOD_SUPERVISOR = "supervisor"
METHOD_SYSTEMD = "systemd"
METHOD_CONTAINER = "container"
METHOD_MANUAL = "manual"

# Said to a person, in the integration's repair notice, so each one names the
# thing to go and do instead.
REASONS = {
    METHOD_SUPERVISOR: "Supervisor updates this add-on from the add-on store.",
    METHOD_CONTAINER: (
        "A container cannot replace its own image. Pull the new tag and "
        "recreate the container."
    ),
    METHOD_MANUAL: (
        "This install has no updater unit. Re-run scripts/install-proxy.sh on "
        "the proxy host to add one."
    ),
}


class UpdateUnavailableError(RuntimeError):
    """This install has no way to update itself."""


class UpdateBusyError(RuntimeError):
    """An update is already running."""


@lru_cache(maxsize=1)
def detect_method() -> str:
    """Name how this install gets new code.

    Cached for the life of the process, which is also exactly as long as the
    answer can change: installing the updater unit means running
    install-proxy.sh, and that restarts this service. Without the cache every
    /status poll would stat three paths on the event loop.
    """
    # Supervisor first. The add-on is a container too, and "Supervisor owns
    # this" is the more useful of the two true answers.
    if os.getenv("SUPERVISOR_TOKEN"):
        return METHOD_SUPERVISOR
    if (
        UPDATE_UNIT_FILE.exists()
        and UPDATE_SCRIPT.exists()
        and shutil.which("systemctl")
    ):
        return METHOD_SYSTEMD
    if DOCKER_MARKER.exists():
        return METHOD_CONTAINER
    return METHOD_MANUAL


def describe() -> dict[str, Any]:
    """What /status tells the integration about updating this install."""
    method = detect_method()
    supported = method == METHOD_SYSTEMD
    described: dict[str, Any] = {"method": method, "supported": supported}
    if not supported:
        described["reason"] = REASONS[method]
    return described


async def _systemctl(*args: str) -> int:
    process = await asyncio.create_subprocess_exec(
        "systemctl",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await process.wait()


async def is_running() -> bool:
    """Whether the updater unit is mid-run."""
    return await _systemctl("is-active", "--quiet", UPDATE_UNIT) == 0


async def start() -> dict[str, Any]:
    """Start the installed updater unit. Takes no arguments, deliberately.

    Nothing about what gets installed comes from the caller: no tag, no ref, no
    repository URL. The unit, and the script it runs, were fixed at install
    time. An endpoint that accepted a version to install would be a way to run
    chosen code as root on the camera host wearing the clothes of an update
    button, so there is no parameter here to add one to.

    `--no-block`, on a unit of its own, is the other load-bearing detail.
    bootstrap.sh restarts blink-liveview-proxy.service, and systemd stops a
    service by killing its whole cgroup: an updater running as a child of this
    process would kill itself halfway through the upgrade it was performing.
    Started this way it belongs to its own unit and survives the restart it
    causes.
    """
    method = detect_method()
    if method != METHOD_SYSTEMD:
        raise UpdateUnavailableError(REASONS[method])
    if await is_running():
        raise UpdateBusyError("An update is already running.")

    code = await _systemctl("start", "--no-block", UPDATE_UNIT)
    if code != 0:
        raise UpdateUnavailableError(
            f"systemctl start {UPDATE_UNIT} exited {code}. "
            f"Check: journalctl -u {UPDATE_UNIT}"
        )

    LOGGER.info("Started %s at the integration's request", UPDATE_UNIT)
    # "Started", not "updated": the unit exits early when the newest tag is
    # already installed, and the integration confirms by watching the version
    # on /status change, not by believing this.
    return {"started": True, "method": METHOD_SYSTEMD, "unit": UPDATE_UNIT}
